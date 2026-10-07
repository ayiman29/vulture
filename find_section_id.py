import argparse
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


FEED_URL = "https://usis-cdn.eniamza.com/connect.json"
ENV_PATH = Path(__file__).with_name(".env")


def normalize_section(section):
    section = section.strip()
    if not section:
        return ""

    try:
        return str(int(section))
    except ValueError:
        return section.casefold()


def find_section_id(records, course_code, section):
    wanted_course = course_code.strip().casefold()
    wanted_section = normalize_section(section)

    for record in records:
        if not isinstance(record, dict):
            continue

        record_course = str(record.get("courseCode", "")).strip().casefold()
        record_section = normalize_section(
            str(record.get("sectionName", ""))
        )

        if (
            record_course == wanted_course
            and record_section == wanted_section
        ):
            return record.get("sectionId")

    return None


def fetch_records():
    request = Request(
        FEED_URL,
        headers={"User-Agent": "vulture-section-id-helper/1.0"},
    )
    with urlopen(request, timeout=30) as response:
        records = json.load(response)

    if not isinstance(records, list):
        raise ValueError("The feed did not contain a JSON array.")

    return records


def save_section_id(section_id):
    lines = (
        ENV_PATH.read_text(encoding="utf-8").splitlines()
        if ENV_PATH.is_file()
        else []
    )
    replacement = f"SECTION_ID={section_id}"
    updated_lines = []
    found = False

    for line in lines:
        if line.strip().startswith("SECTION_ID="):
            if not found:
                updated_lines.append(replacement)
                found = True
        else:
            updated_lines.append(line)

    if not found:
        updated_lines.append(replacement)

    ENV_PATH.write_text(
        "\n".join(updated_lines) + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Find a BRACU sectionId by course code and section."
    )
    parser.add_argument("course_code", help="Course code, such as CSE230")
    parser.add_argument("section", help="Section, such as 5 or 05")
    args = parser.parse_args()

    try:
        section_id = find_section_id(
            fetch_records(),
            args.course_code,
            args.section,
        )
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Could not load the course feed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Invalid course feed: {exc}", file=sys.stderr)
        return 1

    if section_id is None:
        print(
            f"No section found for {args.course_code} section {args.section}.",
            file=sys.stderr,
        )
        return 1

    try:
        save_section_id(section_id)
    except OSError as exc:
        print(f"Could not update {ENV_PATH}: {exc}", file=sys.stderr)
        return 1

    print(section_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
