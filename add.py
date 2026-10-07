import base64
import json
import os
from pathlib import Path
import time
import threading

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


# ============================================================
# CONFIG
# ============================================================


def load_env_file():
    env_path = Path(__file__).with_name(".env")
    if not env_path.is_file():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("\"'")
        if name:
            os.environ.setdefault(name, value)


def get_required_id(name):
    value = os.getenv(name)
    if value is None:
        raise RuntimeError(
            f"Required environment variable {name} is not set."
        )

    try:
        identifier = int(value)
    except ValueError as exc:
        raise RuntimeError(
            f"Environment variable {name} must be an integer."
        ) from exc

    if identifier <= 0:
        raise RuntimeError(
            f"Environment variable {name} must be greater than zero."
        )

    return identifier


load_env_file()
STUDENT_PORTFOLIO_ID = get_required_id("STUDENT_PORTFOLIO_ID")
SECTION_ID = get_required_id("SECTION_ID")


API_URL = "https://connect.bracu.ac.bd/api/adv/v1/student-courses"

BRACU_PAGE = (
    "https://connect.bracu.ac.bd/"
    "student/advising/self-registration"
)

# Authentication expiry is only examined this often.
AUTH_CHECK_INTERVAL = 30

# Refresh BRACU when token has this much time left.
TOKEN_REFRESH_THRESHOLD = 60

# Wait before refreshing after a navigation is interrupted by SSO.
SSO_REFRESH_DELAY_MS = 30_000

# Persistent Playwright browser profile
PROFILE_DIR = "bracu_playwright_profile_other_email"


# ============================================================
# AUTH STATE
# ============================================================

auth_token = None
advising_session = None
token_expiry = None

auth_lock = threading.Lock()
section_update = threading.Event()


# ============================================================
# JWT EXPIRY
# ============================================================

def get_jwt_expiry(token):
    """
    Extract the 'exp' field from a JWT.

    This does NOT verify the signature.
    We only use it to know when we should refresh.
    """

    try:
        parts = token.split(".")

        if len(parts) != 3:
            return None

        payload = parts[1]

        # JWT base64 sometimes doesn't contain padding.
        payload += "=" * (-len(payload) % 4)

        decoded = base64.urlsafe_b64decode(
            payload
        ).decode("utf-8")

        data = json.loads(decoded)

        return data.get("exp")

    except Exception:
        return None


# ============================================================
# CAPTURE AUTH FROM BROWSER
# ============================================================

def capture_request(request):
    """
    Whenever the BRACU browser makes an authenticated request,
    capture the newest token/session information.
    """

    global auth_token
    global advising_session
    global token_expiry
    if "connect.bracu.ac.bd" not in request.url:
        return

    headers = request.headers

    authorization = headers.get("authorization")
    session = headers.get("x-advising-session")

    with auth_lock:

        if (
            authorization
            and authorization.lower().startswith("bearer ")
        ):
            auth_token = authorization

            token_expiry = get_jwt_expiry(
                authorization.split(" ", 1)[1]
            )

            if token_expiry:
                remaining = (
                    token_expiry - time.time()
                )

                print(
                    f"\n[AUTH] New token captured "
                    f"({int(remaining)}s remaining)"
                )

        if session:
            advising_session = session


def handle_section_event(params):
    """Signal the main loop for a matching message from the browser's SSE."""

    print(
        "[EVENTS] SSE event: "
        f"event={params.get('eventName')!r}, "
        f"id={params.get('eventId')!r}, "
        f"data={params.get('data', '')}"
    )

    if params.get("eventName") not in (None, "message"):
        return

    try:
        event = json.loads(params["data"])
        if isinstance(event, str):
            event = json.loads(event)

        records = event.get("data", event)
        if isinstance(records, str):
            records = json.loads(records)
        if isinstance(records, dict):
            records = [records]

        for record in records:
            if not isinstance(record, dict):
                continue

            if (
                str(record.get("section")) == str(SECTION_ID)
                and record.get("delta") == -1
            ):
                print(
                    f"[EVENTS] Target section {SECTION_ID} "
                    "reported delta -1."
                )
                section_update.set()
                return

    except (KeyError, TypeError, ValueError, AttributeError):
        return


# ============================================================
# AUTH STATUS
# ============================================================

def seconds_until_token_expiry():

    with auth_lock:

        if not token_expiry:
            return None

        return token_expiry - time.time()


def have_auth():

    with auth_lock:
        return auth_token is not None


def navigate_to_bracu_login(page):
    """Navigate to BRACU while tolerating a concurrent SSO redirect."""

    try:
        page.goto(
            BRACU_PAGE,
            wait_until="domcontentloaded",
        )
        return

    except PlaywrightError as exc:

        if "interrupted by another navigation" not in str(exc):
            raise

        print(
            "[AUTH] Navigation was redirected by SSO; "
            f"waiting {SSO_REFRESH_DELAY_MS // 1000}s before refreshing..."
        )
        page.wait_for_timeout(SSO_REFRESH_DELAY_MS)

        try:
            page.reload(wait_until="domcontentloaded")
        except PlaywrightError as reload_exc:
            if "interrupted by another navigation" not in str(
                reload_exc
            ):
                raise

            print(
                "[AUTH] Refresh was also redirected by SSO; "
                "continuing with the current page."
            )


def click_google_login(page):
    """Click the Google identity-provider button when BRACU shows it."""

    try:
        google_button = page.locator("#social-google")
        google_button.wait_for(
            state="visible",
            timeout=8_000,
        )
        print("[AUTH] Clicking 'Sign in with Google' automatically...")
        google_button.click()

    except PlaywrightTimeoutError:
        print(
            "[AUTH] Google sign-in button not shown; "
            "assuming SSO is already redirecting."
        )


# ============================================================
# REFRESH BRACU SESSION
# ============================================================

def refresh_bracu_session(page):

    global auth_token
    global advising_session
    global token_expiry

    print("\n----------------------------------------")
    print("Refreshing BRACU authentication...")
    print("----------------------------------------")

    with auth_lock:
        auth_token = None
        advising_session = None
        token_expiry = None

    # This is the actual website interaction.
    # It only happens when authentication needs refreshing.
    navigate_to_bracu_login(page)
    click_google_login(page)

    print("Waiting for authenticated BRACU request...")

    # Give BRACU's frontend some time to make requests.
    for _ in range(20):

        if have_auth():
            break

        page.wait_for_timeout(1000)

    # If login is required, let the user do it.
    if not have_auth():

        print()
        print("BRACU requires authentication.")
        print(
            "Log in manually in the browser window."
        )

        input(
            "Press ENTER after you have finished logging in..."
        )

        # Give the newly authenticated application
        # a chance to make an API request.
        for _ in range(20):

            if have_auth():
                break

            page.wait_for_timeout(1000)

    if not have_auth():
        print(
            "[AUTH] Failed to obtain authentication."
        )

        return False

    remaining = seconds_until_token_expiry()

    if remaining is not None:

        print(
            f"[AUTH] Authentication ready "
            f"({int(remaining)}s remaining)"
        )

    else:

        print(
            "[AUTH] Authentication captured."
        )

    return True


# ============================================================
# TAKE COURSE
# ============================================================

def take_course(context):

    global auth_token
    global advising_session

    with auth_lock:

        current_token = auth_token
        current_session = advising_session

    if not current_token:

        print(
            "[API] No authentication token."
        )

        return None

    payload = {
        "studentPortfolioId": STUDENT_PORTFOLIO_ID,
        "sectionId": SECTION_ID
    }

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",

        "Authorization": current_token,

        "X-Realm": "bracu",
        "X-Source": "3",

        "Origin": "https://connect.bracu.ac.bd",
        "Referer": BRACU_PAGE,

        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/154.0.0.0 Safari/537.36"
        ),
    }

    if current_session:

        headers[
            "X-Advising-Session"
        ] = current_session

    print()
    print("========================================")
    print("SEAT DETECTED")
    print("Attempting course registration...")
    print("========================================")

    print(
        json.dumps(
            payload,
            indent=2
        )
    )

    try:

        response = context.request.post(
            API_URL,
            headers=headers,
            data=payload
        )

    except Exception as e:

        print(
            f"[API ERROR] {e}"
        )

        return None

    print(
        f"[API] HTTP {response.status}"
    )

    try:

        result = response.json()

        print(
            json.dumps(
                result,
                indent=2
            )
        )

    except Exception:

        print(
            response.text()
        )

    return response


def course_is_enrolled(context):
    """Check the enrolled-course list for the target section."""

    with auth_lock:
        current_token = auth_token
        current_session = advising_session

    if not current_token:
        print("[API] No authentication token for enrollment verification.")
        return None

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": current_token,
        "X-Realm": "bracu",
        "X-Source": "3",
        "Origin": "https://connect.bracu.ac.bd",
        "Referer": BRACU_PAGE,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/154.0.0.0 Safari/537.36"
        ),
    }

    if current_session:
        headers["X-Advising-Session"] = current_session

    try:
        response = context.request.get(
            f"{API_URL}/{STUDENT_PORTFOLIO_ID}",
            headers=headers,
        )
    except Exception as e:
        print(f"[API ERROR] Enrollment verification failed: {e}")
        return None

    print(f"[API] Enrollment verification HTTP {response.status}")

    if response.status in (401, 403):
        print("[API] Authentication rejected during enrollment verification.")
        return None

    if not 200 <= response.status < 300:
        print("[API] Enrollment verification request did not succeed.")
        return None

    try:
        sections = response.json()
    except Exception:
        print("[API] Enrollment verification returned invalid JSON.")
        return None

    if not isinstance(sections, list):
        print("[API] Enrollment verification returned no course list.")
        return False

    for section in sections:
        if not isinstance(section, dict):
            continue

        try:
            section_id = int(section.get("sectionId"))
        except (TypeError, ValueError):
            continue

        if section_id == SECTION_ID:
            return True

    return False


# ============================================================
# MAIN
# ============================================================

with sync_playwright() as p:

    context = p.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=False
    )

    page = (
        context.pages[0]
        if context.pages
        else context.new_page()
    )

    # Continuously observe browser requests.
    page.on(
        "request",
        capture_request
    )

    cdp = context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.on(
        "Network.eventSourceMessageReceived",
        handle_section_event,
    )

    # --------------------------------------------------------
    # INITIAL AUTHENTICATION
    # --------------------------------------------------------

    if not refresh_bracu_session(page):

        print(
            "\nCould not authenticate with BRACU."
        )

        context.close()
        raise SystemExit(1)

    print()
    print("========================================")
    print("MONITOR STARTED")
    print("========================================")
    print(
        f"Student Portfolio: {STUDENT_PORTFOLIO_ID}"
    )
    print(
        f"Target Section:    {SECTION_ID}"
    )
    print(
        "Seat detection:    Mercure event stream"
    )
    print(
        f"Auth check:        {AUTH_CHECK_INTERVAL}s"
    )
    print(
        f"Refresh threshold: {TOKEN_REFRESH_THRESHOLD}s"
    )
    print("========================================")
    print()

    last_auth_check = 0
    try:

        while True:

            now = time.time()

            # =================================================
            # AUTHENTICATION CHECK
            # =================================================

            if (
                now - last_auth_check
                >= AUTH_CHECK_INTERVAL
            ):

                last_auth_check = now

                remaining = (
                    seconds_until_token_expiry()
                )

                if remaining is None:

                    print(
                        "[AUTH] No valid expiry "
                        "information. Refreshing..."
                    )

                    refresh_bracu_session(page)

                elif remaining <= 0:

                    print(
                        "[AUTH] Token expired."
                    )

                    refresh_bracu_session(page)

                elif (
                    remaining
                    <= TOKEN_REFRESH_THRESHOLD
                ):

                    print(
                        f"[AUTH] Token expires in "
                        f"{int(remaining)}s. "
                        f"Refreshing..."
                    )

                    refresh_bracu_session(page)

                else:

                    print(
                        f"[AUTH] OK "
                        f"({int(remaining)}s remaining)"
                    )

            # =================================================
            # MERCURE SECTION UPDATE
            # =================================================

            page.wait_for_timeout(250)

            if section_update.is_set():

                section_update.clear()

                response = take_course(
                    context
                )

                # ------------------------------------------------
                # AUTH EXPIRED
                # ------------------------------------------------

                if response is not None and response.status in (
                    401,
                    403
                ):

                    print()
                    print(
                        "[API] Authentication rejected."
                    )

                    if refresh_bracu_session(page):

                        print(
                            "[API] Retrying registration..."
                        )

                        response = take_course(
                            context
                        )

                    else:

                        print(
                            "[AUTH] Refresh failed."
                        )

                # ------------------------------------------------
                # SUCCESS
                # ------------------------------------------------

                if (
                    response is not None
                    and 200 <= response.status < 300
                ):

                    enrolled = course_is_enrolled(context)

                    if enrolled:

                        print()
                        print(
                            "========================================"
                        )
                        print(
                            "COURSE REQUEST SUCCEEDED AND WAS VERIFIED"
                        )
                        print(
                            "Stopping script."
                        )
                        print(
                            "========================================"
                        )

                        break

                    if enrolled is False:

                        print(
                            "[API] Add returned success, but the course "
                            "is not enrolled. Returning to seat listening."
                        )

                    else:

                        print(
                            "[API] Could not verify enrollment; "
                            "waiting for the next seat event."
                        )

                else:

                    print(
                        "[API] Registration did not succeed."
                    )

    except KeyboardInterrupt:

        print(
            "\nStopped by user."
        )

    finally:
        context.close()