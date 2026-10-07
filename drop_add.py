import base64
import json
import os
from pathlib import Path
import threading
import time

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


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


def get_required_ids(name):
    value = os.getenv(name)
    if value is None:
        raise RuntimeError(
            f"Required environment variable {name} is not set."
        )

    identifiers = tuple(
        get_required_id_value(name, item.strip())
        for item in value.split(",")
        if item.strip()
    )
    if not identifiers:
        raise RuntimeError(
            f"Environment variable {name} must contain at least one ID."
        )

    return identifiers


def get_required_id_value(name, value):
    try:
        identifier = int(value)
    except ValueError as exc:
        raise RuntimeError(
            f"Environment variable {name} must contain only integers."
        ) from exc

    if identifier <= 0:
        raise RuntimeError(
            f"Environment variable {name} must contain positive integers."
        )

    return identifier


load_env_file()

API_URL = "https://connect.bracu.ac.bd/api/adv/v1/student-courses"
BRACU_PAGE = (
    "https://connect.bracu.ac.bd/"
    "student/advising/self-registration"
)
PROFILE_DIR = "bracu_playwright_profile_other_email"

STUDENT_PORTFOLIO_ID = get_required_id("STUDENT_PORTFOLIO_ID")
CURRENT_SECTION_ID = get_required_id("SECTION_ID")
TARGET_SECTION_IDS = get_required_ids("TARGET_SECTION_IDS")


AUTH_CHECK_INTERVAL = 30
TOKEN_REFRESH_THRESHOLD = 60
SSO_REFRESH_DELAY_MS = 30_000
VERIFICATION_ATTEMPTS = 3
VERIFICATION_DELAY_MS = 500

auth_token = None
advising_session = None
token_expiry = None
current_course_enrolled = True
auth_lock = threading.Lock()
section_update = threading.Event()


def get_jwt_expiry(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(
            base64.urlsafe_b64decode(payload).decode("utf-8")
        ).get("exp")
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def capture_request(request):
    global auth_token, advising_session, token_expiry

    if "connect.bracu.ac.bd" not in request.url:
        return

    headers = request.headers
    authorization = headers.get("authorization")
    session = headers.get("x-advising-session")

    with auth_lock:
        if authorization and authorization.lower().startswith("bearer "):
            auth_token = authorization
            token_expiry = get_jwt_expiry(authorization.split(" ", 1)[1])
            if token_expiry:
                print(
                    f"\n[AUTH] New token captured "
                    f"({int(token_expiry - time.time())}s remaining)"
                )
        if session:
            advising_session = session


def handle_section_event(params):
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
            if (
                isinstance(record, dict)
                and str(record.get("section")) in {
                    str(section_id) for section_id in TARGET_SECTION_IDS
                }
                and record.get("delta") == -1
            ):
                print(
                    f"[EVENTS] Target section {record['section']} "
                    "reported an available seat."
                )
                section_update.set()
                return
    except (KeyError, TypeError, ValueError, AttributeError):
        return


def seconds_until_token_expiry():
    with auth_lock:
        return None if token_expiry is None else token_expiry - time.time()


def have_auth():
    with auth_lock:
        return auth_token is not None


def navigate_to_bracu(page):
    try:
        page.goto(BRACU_PAGE, wait_until="domcontentloaded")
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
            if "interrupted by another navigation" not in str(reload_exc):
                raise
            print(
                "[AUTH] Refresh was also redirected by SSO; "
                "continuing with the current page."
            )


def refresh_bracu_session(page):
    global auth_token, advising_session, token_expiry

    with auth_lock:
        auth_token = None
        advising_session = None
        token_expiry = None

    navigate_to_bracu(page)
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

    print("Waiting for authenticated BRACU request...")
    for _ in range(20):
        if have_auth():
            return True
        page.wait_for_timeout(1000)

    print(
        "BRACU requires authentication in the browser window. "
        "Waiting for login without requiring keyboard input..."
    )
    for _ in range(120):
        if have_auth():
            return True
        page.wait_for_timeout(1000)

    print("[AUTH] Failed to capture a browser token and advising session.")
    return False


def request_headers():
    with auth_lock:
        current_token = auth_token
        current_session = advising_session

    if not current_token:
        return None

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Authorization": current_token,
        "X-Realm": "bracu",
        "X-Source": "3",
        "Origin": "https://connect.bracu.ac.bd",
        "Referer": BRACU_PAGE,
    }
    if current_session:
        headers["X-Advising-Session"] = current_session
    return headers


def log_response(label, response):
    print(f"[API] {label}: HTTP {response.status}")
    try:
        print(json.dumps(response.json(), indent=2))
    except (TypeError, ValueError):
        print(response.text())


def drop_course(context):
    headers = request_headers()
    if headers is None:
        return None
    payload = {
        "studentPortfolioId": STUDENT_PORTFOLIO_ID,
        "sectionId": CURRENT_SECTION_ID,
    }
    try:
        response = context.request.delete(API_URL, headers=headers, data=payload)
    except PlaywrightError as exc:
        print(f"[API] Drop request failed: {exc}")
        return None
    log_response("drop", response)
    return response


def add_course(context, section_id):
    headers = request_headers()
    if headers is None:
        return None
    payload = {
        "studentPortfolioId": STUDENT_PORTFOLIO_ID,
        "sectionId": section_id,
    }
    try:
        response = context.request.post(API_URL, headers=headers, data=payload)
    except PlaywrightError as exc:
        print(f"[API] Add request failed: {exc}")
        return None
    log_response(f"add section {section_id}", response)
    return response


def enrolled_sections(context):
    global current_course_enrolled

    headers = request_headers()
    if headers is None:
        return None
    try:
        response = context.request.get(
            f"{API_URL}/{STUDENT_PORTFOLIO_ID}",
            headers=headers,
        )
    except PlaywrightError as exc:
        print(f"[API] Verification request failed: {exc}")
        return None
    log_response("verify enrollment", response)
    if not 200 <= response.status < 300:
        return None
    try:
        result = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(result, list):
        return []

    current_course_enrolled = any(
        isinstance(section, dict)
        and section.get("sectionId") is not None
        and int(section["sectionId"]) == CURRENT_SECTION_ID
        for section in result
    )
    return result


def find_target_section(context):
    for attempt in range(1, VERIFICATION_ATTEMPTS + 1):
        sections = enrolled_sections(context)
        if sections is not None:
            enrolled_ids = {
                int(section["sectionId"])
                for section in sections
                if isinstance(section, dict) and section.get("sectionId") is not None
            }
            match = enrolled_ids.intersection(TARGET_SECTION_IDS)
            if match:
                return match.pop()
        if attempt < VERIFICATION_ATTEMPTS:
            time.sleep(VERIFICATION_DELAY_MS / 1000)
    return None


def replace_course(context):
    global current_course_enrolled

    if not current_course_enrolled:
        print(
            "[API] Current section is not enrolled; "
            "skipping the drop request."
        )
        return False

    dropped = drop_course(context)
    if dropped is None or not 200 <= dropped.status < 300:
        print("[API] Drop was not confirmed; skipping add to avoid losing the course.")
        return False
    current_course_enrolled = False

    for target_section_id in TARGET_SECTION_IDS:
        add_course(context, target_section_id)
        enrolled_target = find_target_section(context)
        if enrolled_target is not None:
            print(f"[API] Verified target section {enrolled_target} is enrolled.")
            return True

    print("[API] Target was not verified; restoring the dropped section.")
    restored = add_course(context, CURRENT_SECTION_ID)
    if restored is not None and 200 <= restored.status < 300:
        print("[API] Restore request sent; verifying the current section.")
        if CURRENT_SECTION_ID in {
            int(section["sectionId"])
            for section in (enrolled_sections(context) or [])
            if isinstance(section, dict) and section.get("sectionId") is not None
        }:
            print("[API] Dropped section restored.")
    return False


def main():
    if CURRENT_SECTION_ID in TARGET_SECTION_IDS:
        raise SystemExit("The current section must not also be a target section.")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.on("request", capture_request)
        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.on("Network.eventSourceMessageReceived", handle_section_event)

        try:
            if not refresh_bracu_session(page):
                raise SystemExit(1)
            print()
            print("========================================")
            print("MONITOR STARTED")
            print("========================================")
            print(f"Current section: {CURRENT_SECTION_ID}")
            print(f"Target sections: {TARGET_SECTION_IDS}")
            print("Seat detection:  Mercure event stream")
            print(f"Auth check:      {AUTH_CHECK_INTERVAL}s")
            print("========================================")
            print()
            print(
                f"Monitoring targets {TARGET_SECTION_IDS}; "
                f"current section is {CURRENT_SECTION_ID}."
            )

            last_auth_check = 0
            while True:
                now = time.time()
                if now - last_auth_check >= AUTH_CHECK_INTERVAL:
                    last_auth_check = now
                    remaining = seconds_until_token_expiry()
                    if remaining is None:
                        print("[AUTH] No valid expiry information. Refreshing...")
                        if not refresh_bracu_session(page):
                            continue
                    elif remaining <= 0:
                        print("[AUTH] Token expired. Refreshing...")
                        if not refresh_bracu_session(page):
                            continue
                    elif remaining <= TOKEN_REFRESH_THRESHOLD:
                        print(
                            f"[AUTH] Token expires in {int(remaining)}s. "
                            "Refreshing..."
                        )
                        if not refresh_bracu_session(page):
                            continue
                    else:
                        print(f"[AUTH] OK ({int(remaining)}s remaining)")

                page.wait_for_timeout(250)
                if section_update.is_set():
                    section_update.clear()
                    if replace_course(context):
                        break
        except KeyboardInterrupt:
            print("\nStopped by user.")
        finally:
            context.close()


main()