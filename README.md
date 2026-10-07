# VULTURE

This project contains two Playwright-based scripts for BRACU self-registration:

- `add.py` waits for a seat event for one section and attempts to register it.
- `drop_add.py` monitors for target sections, drops the currently enrolled section,
  and attempts to add a target section. If the target cannot be verified, it
  attempts to restore the original section.
- `find_section_id.py` looks up a section ID from the public course feed and
  writes it to `.env`.

Use these scripts only with an account and registration activity you are
authorized to automate. You are responsible for following BRACU's rules and
any applicable policies.

## SO MUCH WORKKKKK(?)

Intentionally inconvenient for OBVIOUS REASONS.

## Requirements

- Windows with Python 3.10 or newer.
- Playwright and its Chromium browser.
- Network access to `connect.bracu.ac.bd`.

## Setup

Open PowerShell in the project directory:

```powershell
cd C:\Users\ayima\PROJECTS\vulture
```

Install Playwright if it is not already installed:

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
```

The scripts use a local persistent browser profile. The profile directory is
configured in the scripts as `bracu_playwright_profile_other_email`. Do not
share that directory: it may contain login cookies and browser session data.

## Configure the IDs

The project calls the student server identifier
`STUDENT_PORTFOLIO_ID`. If you refer to it as `student_server_id`, use the
`STUDENT_PORTFOLIO_ID` key below.

Create or edit `.env` beside the Python files:

```env
STUDENT_PORTFOLIO_ID=17001
SECTION_ID=190120
TARGET_SECTION_IDS=190245
```

The values must be positive integer IDs:

- `STUDENT_PORTFOLIO_ID` identifies the student's BRACU portfolio.
- `SECTION_ID` is the section currently selected for the single-section
  registration flow in `add.py`.
- In `drop_add.py`, `SECTION_ID` is used as `CURRENT_SECTION_ID`, meaning the
  section that may be dropped and restored.
- `TARGET_SECTION_IDS` is a comma-separated list of sections that
  `drop_add.py` should monitor and attempt to add.

Both scripts load `.env` themselves and do not require `python-dotenv`.
Existing process environment variables take precedence over values in `.env`.
Do not commit real account-specific IDs or browser profile data to a public
repository.

## Find Your Student Portfolio ID

1. Log in to [BRACU Connect](https://connect.bracu.ac.bd/).
2. Go to [Self Registration](https://connect.bracu.ac.bd/student/advising/self-registration).

3. Open **Developer Tools** by either:
   - Pressing **F12**, or
   - Right-clicking anywhere on the page and selecting **Inspect**.

4. In the Developer Tools window, open the **Network** tab.

5. Refresh the page (`Ctrl + R`) so that the network requests are captured again.

6. In the Network tab, look for a request containing a 5 to 6 digit number:

<img width="586" height="719" alt="image" src="https://github.com/user-attachments/assets/88f8dc24-b584-4592-83b1-3a06818b3ed6" />



8. The number is your **Student Portfolio ID**.


9. In this example, the **Student Portfolio ID** is `67672`.
10. Put this number in your .env


## Find a section ID


Run the helper with a course code and section number:

```powershell
py find_section_id.py CSE230 5
```

Leading zeroes are ignored, so these are equivalent:

```powershell
py find_section_id.py CSE230 6
py find_section_id.py CSE230 06
```

On success, the helper prints the matching `sectionId` and replaces or adds
the `SECTION_ID` line in `.env`. It preserves the other `.env` entries:

```text
190245
```

If no match is found, it prints an error and does not change `.env`.

## First browser login

1. Confirm that `.env` contains the intended IDs.
2. Start one of the scripts.
3. A visible Chromium window opens.
4. Log in to Connect using the Gmail/Google account associated with your
   BRACU account. The scripts depend on this browser login and will not work
   automatically without it.
5. Complete any remaining SSO steps in that window.
6. Leave the browser profile available for later runs.

The scripts capture the bearer token and advising session from requests made by
the browser. They do not ask you to paste those values into `.env` or source
code. Tokens are refreshed from the browser session when they are near expiry.

## Run the single-section monitor

Use `add.py` when the goal is to register the section in `SECTION_ID`:

```powershell
py add.py
```

The flow is:

1. Load and validate both IDs from `.env`.
2. Open the persistent Chromium profile.
3. Navigate to BRACU self-registration.
4. Capture authentication headers from browser requests.
5. Listen to the browser's Mercure/SSE section events.
6. When a matching event reports an available seat, send the registration
   request for `SECTION_ID`.
7. Re-authenticate and retry if authentication is rejected.
8. Verify that the section appears in the enrolled-course list.
9. Stop only after successful enrollment verification.

Stop it with `Ctrl+C`.

## Run the drop-and-add monitor

Use `drop_add.py` for a controlled section replacement:

```powershell
py drop_add.py
```

Set `TARGET_SECTION_IDS` in `.env` to the sections that should be monitored.
For multiple targets, separate IDs with commas:

```env
TARGET_SECTION_IDS=190245,190311
```

The normal drop-and-add flow is:

1. Load `STUDENT_PORTFOLIO_ID` and `SECTION_ID` from `.env`.
2. Treat `SECTION_ID` as the current section.
3. Confirm that the current section is enrolled.
4. Listen for availability events for the target section IDs.
5. When a target is detected, send a drop request for the current section.
6. Add the target section and verify the enrollment list.
7. Stop after verification succeeds.
8. If the target cannot be verified, attempt to add the original
   `CURRENT_SECTION_ID` back.

The script refuses to start if the current section is also listed as a target
section.

## Example sanitized run

The following is a sanitized reconstruction of the process represented by a
real log. All IDs, student values, UUIDs, timestamps, course names, and
authentication-related values have been changed or removed. It is an example
of the sequence, not a copy of an account log:

```text
========================================
MONITOR STARTED
========================================
Current section: 190120
Target sections: (190245,)
Seat detection:  Mercure event stream
Auth check:      30s
========================================

[EVENTS] SSE event: section=190311, delta=-1
[EVENTS] SSE event: section=190884, delta=1
[EVENTS] SSE event: section=190245, delta=-1, total=37
[EVENTS] Target section 190245 reported an available seat.

[API] drop: HTTP 200
"200 OK"

[AUTH] New token captured (about 160s remaining)
[API] add section 190245: HTTP 200
"200 OK"

[API] verify enrollment: HTTP 200
[
  {
    "courseCode": "CSE###",
    "courseCredit": 3,
    "courseName": "SANITIZED COURSE NAME",
    "sectionId": 190245,
    "sectionName": "07"
  },
  {
    "courseCode": "CSE###L",
    "courseCredit": 0,
    "courseName": "SANITIZED COURSE LAB",
    "parentSectionId": 190245,
    "sectionId": 190246,
    "sectionName": "07"
  }
]

[API] Verified target section 190245 is enrolled.
```

### What happens in this example

1. The browser receives a continuous stream of section-update events. Events
   for other sections are logged but do not trigger a course action.
2. The target section reports a negative seat delta. The monitor interprets
   that event as a seat becoming available for the configured target.
3. `drop_add.py` sends a drop request for the current section and checks the
   HTTP response before continuing.
4. The browser produces a fresh authentication token while the operation is
   in progress. The API request uses the current in-memory authentication
   headers; credentials are not written to `.env`.
5. The script sends an add request for the target section.
6. The script fetches the enrolled-course list and checks for the target
   `sectionId`. A 2xx response alone is not treated as success.
7. The returned list can include a related lab section. The target theory
   section is identified by its own `sectionId`, while `parentSectionId`
   describes the relationship.

If verification fails after the drop, `drop_add.py` attempts to restore the
original current section. The real scripts may print response bodies, student
IDs, or session-related values, so treat terminal output as account-sensitive
and sanitize it before sharing.

## How the code works

### Configuration

`add.py` and `drop_add.py` each read `.env` from the directory containing the
script. The small loader uses `os.environ.setdefault`, so a value explicitly
provided by the process is not overwritten. `get_required_id` rejects missing,
non-numeric, and non-positive values before browser automation starts.

### Authentication capture

Playwright launches Chromium with a persistent profile. A request listener
examines requests to the BRACU host and captures:

- the `Authorization: Bearer ...` header;
- the `X-Advising-Session` header;
- the JWT expiry time, when available.

The values are held in memory only. The scripts periodically check expiry and
refresh the BRACU page when necessary.

### Seat detection

The scripts enable the Chromium DevTools Protocol Network domain and listen for
`Network.eventSourceMessageReceived`. Incoming event data is decoded as JSON.
Only events for the configured section IDs and the expected seat delta are
used to trigger an API action.

### Registration API calls

After a matching event, the scripts use the captured browser authentication
headers to call the BRACU student-courses API:

```text
https://connect.bracu.ac.bd/api/adv/v1/student-courses
```

The request payload contains a student portfolio ID and a section ID. A
successful HTTP status is not enough on its own; the scripts query the
student's enrolled-course list and compare returned `sectionId` values.

### Recovery behavior

`add.py` retries after an authentication failure when it can refresh the
browser session. `drop_add.py` verifies the drop before adding a target. If a
target add cannot be verified, it attempts to restore the original section
instead of continuing to remove courses.

## Troubleshooting

### Missing or invalid ID error

Check `.env` for both keys and ensure each value is a positive integer:

```env
STUDENT_PORTFOLIO_ID=17001
SECTION_ID=190120
TARGET_SECTION_IDS=190245
```

### No section found

Use the exact course code and section shown by the course feed. Both `6` and
`06` are normalized to the same numeric section. A course code is matched
case-insensitively.

### Authentication fails

Close stale browser windows, start the script again, and complete login in the
visible Chromium window. Do not copy cookies or bearer tokens into source code.

### The script reports success but enrollment is absent

The scripts already perform a follow-up enrollment check. Review the printed
HTTP status and response body, verify the selected IDs, and do not assume that
an HTTP 2xx response alone means the course was registered.

## Files

| File | Purpose |
| --- | --- |
| `add.py` | Monitor one section and register it when a seat event arrives |
| `drop_add.py` | Monitor target sections and replace the current section |
| `find_section_id.py` | Look up a section ID and update `.env` |
| `.env` | Local student portfolio and section configuration |
| `bracu_playwright_profile_other_email/` | Persistent browser session data; keep private |

## WARNING: DON'T USE IT

Students are sitting in front of their PCs all day to get their desired
course. Don't break their heart by brutally human-mogging them with your
computer.
