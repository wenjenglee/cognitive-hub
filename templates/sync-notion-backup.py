#!/usr/bin/env python3
"""
Notion → Markdown backup script for Cognitive Hub.

Exports all pages under specified Notion sections (e.g. Rules, SOP, References)
to local markdown files, providing offline access and vendor-neutral portability.

Setup:
    1. Create a Notion Internal Integration at https://www.notion.so/profile/integrations
       - Capabilities: Read content only
    2. Share your Hub page (and all child pages) with the integration
    3. Create a .env file in the same directory as this script:
           NOTION_API_TOKEN=ntn_your_token_here
    4. Edit SECTIONS below with your own page IDs

Usage:
    python3 sync-notion-backup.py              # sync all sections
    python3 sync-notion-backup.py --dry-run    # preview without writing
    python3 sync-notion-backup.py --commit     # sync + git commit + push

Finding your page IDs:
    Open a Notion page → Share → Copy link
    The URL looks like: https://www.notion.so/Your-Page-Title-<32-char-hex-id>
    Copy the 32-character hex ID at the end.
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration — edit these to match your Hub
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent  # adjust if your script lives elsewhere

# Each section maps a Notion parent page to a local directory.
# Replace the page_id values with your own Notion page IDs.
SECTIONS = {
    "rules": {
        "page_id": "YOUR_RULES_PAGE_ID",  # e.g. "3249af7a822c81f6b68ee3a5003c105d"
        "dir": "rules",
    },
    "sop": {
        "page_id": "YOUR_SOP_PAGE_ID",
        "dir": "sop",
    },
    "references": {
        "page_id": "YOUR_REFERENCES_PAGE_ID",
        "dir": "references",
    },
}

# Optional: also export the Hub main page itself
HUB_PAGE_ID = "YOUR_HUB_PAGE_ID"  # set to None to skip

# ---------------------------------------------------------------------------
# Notion API helpers
# ---------------------------------------------------------------------------


def get_token() -> str:
    token = os.environ.get("NOTION_API_TOKEN")
    if token:
        return token
    for env_path in [SCRIPT_DIR / ".env", REPO_ROOT / ".env"]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("NOTION_API_TOKEN="):
                    return line.split("=", 1)[1].strip()
    print("ERROR: NOTION_API_TOKEN not found. Create a .env file or set the env var.")
    sys.exit(1)


def notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
    }


def fetch_child_pages(page_id: str, headers: dict) -> list[dict]:
    """Get child page blocks from a parent page."""
    pages = []
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    params = {"page_size": 100}
    while True:
        r = requests.get(url, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
        for block in data["results"]:
            if block["type"] == "child_page":
                pages.append({
                    "id": block["id"].replace("-", ""),
                    "title": block["child_page"]["title"],
                })
        if not data.get("has_more"):
            break
        params["start_cursor"] = data["next_cursor"]
    return pages


def fetch_blocks(block_id: str, headers: dict) -> list[dict]:
    """Recursively fetch all blocks under a given block."""
    blocks = []
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    params = {"page_size": 100}
    while True:
        r = requests.get(url, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
        for block in data["results"]:
            blocks.append(block)
            if block.get("has_children") and block["type"] not in ("child_page", "child_database"):
                block["_children"] = fetch_blocks(block["id"], headers)
        if not data.get("has_more"):
            break
        params["start_cursor"] = data["next_cursor"]
    return blocks


def fetch_page_title(page_id: str, headers: dict) -> str:
    r = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
    r.raise_for_status()
    parts = r.json().get("properties", {}).get("title", {}).get("title", [])
    return "".join(p.get("plain_text", "") for p in parts)


# ---------------------------------------------------------------------------
# Block → Markdown conversion
# ---------------------------------------------------------------------------


def rich_text_to_md(rich_text: list) -> str:
    parts = []
    for seg in rich_text:
        text = seg.get("plain_text", "")
        ann = seg.get("annotations", {})
        href = seg.get("href") or (seg.get("text", {}).get("link") or {}).get("url")
        if ann.get("code"):
            text = f"`{text}`"
        if ann.get("bold"):
            text = f"**{text}**"
        if ann.get("italic"):
            text = f"*{text}*"
        if ann.get("strikethrough"):
            text = f"~~{text}~~"
        if href:
            text = f"[{text}]({href})"
        parts.append(text)
    return "".join(parts)


def blocks_to_markdown(blocks: list, indent: int = 0) -> str:
    lines = []
    prefix = "  " * indent
    numbered_counter = 0

    for block in blocks:
        btype = block["type"]
        content = block.get(btype, {})

        if btype == "paragraph":
            text = rich_text_to_md(content.get("rich_text", []))
            lines.append(f"{prefix}{text}")
            lines.append("")

        elif btype in ("heading_1", "heading_2", "heading_3"):
            level = int(btype[-1])
            text = rich_text_to_md(content.get("rich_text", []))
            lines.append(f"{'#' * level} {text}")
            lines.append("")

        elif btype == "bulleted_list_item":
            numbered_counter = 0
            text = rich_text_to_md(content.get("rich_text", []))
            lines.append(f"{prefix}- {text}")
            if block.get("_children"):
                lines.append(blocks_to_markdown(block["_children"], indent + 1).rstrip())

        elif btype == "numbered_list_item":
            numbered_counter += 1
            text = rich_text_to_md(content.get("rich_text", []))
            lines.append(f"{prefix}{numbered_counter}. {text}")
            if block.get("_children"):
                lines.append(blocks_to_markdown(block["_children"], indent + 1).rstrip())

        elif btype == "to_do":
            text = rich_text_to_md(content.get("rich_text", []))
            checked = "x" if content.get("checked") else " "
            lines.append(f"{prefix}- [{checked}] {text}")

        elif btype == "toggle":
            text = rich_text_to_md(content.get("rich_text", []))
            lines.append(f"{prefix}<details>")
            lines.append(f"{prefix}<summary>{text}</summary>")
            lines.append("")
            if block.get("_children"):
                lines.append(blocks_to_markdown(block["_children"], indent).rstrip())
            lines.append(f"{prefix}</details>")
            lines.append("")

        elif btype == "code":
            text = rich_text_to_md(content.get("rich_text", []))
            lang = content.get("language", "")
            lines.append(f"{prefix}```{lang}")
            lines.append(text)
            lines.append(f"{prefix}```")
            lines.append("")

        elif btype == "quote":
            text = rich_text_to_md(content.get("rich_text", []))
            for line in text.split("\n"):
                lines.append(f"{prefix}> {line}")
            lines.append("")

        elif btype == "callout":
            icon = content.get("icon", {}).get("emoji", "")
            text = rich_text_to_md(content.get("rich_text", []))
            lines.append(f"{prefix}> {icon} {text}")
            if block.get("_children"):
                child_md = blocks_to_markdown(block["_children"], indent).rstrip()
                for line in child_md.split("\n"):
                    lines.append(f"{prefix}> {line}")
            lines.append("")

        elif btype == "divider":
            lines.append(f"{prefix}---")
            lines.append("")

        elif btype == "table":
            rows = block.get("_children", [])
            if rows:
                table_data = []
                for row in rows:
                    cells = row.get("table_row", {}).get("cells", [])
                    table_data.append([rich_text_to_md(cell) for cell in cells])
                if table_data:
                    lines.append(f"{prefix}| " + " | ".join(table_data[0]) + " |")
                    lines.append(f"{prefix}| " + " | ".join(["---"] * len(table_data[0])) + " |")
                    for row_data in table_data[1:]:
                        lines.append(f"{prefix}| " + " | ".join(row_data) + " |")
                    lines.append("")

        elif btype == "bookmark":
            url = content.get("url", "")
            caption = rich_text_to_md(content.get("caption", []))
            lines.append(f"{prefix}[{caption or url}]({url})")
            lines.append("")

        elif btype == "image":
            caption = rich_text_to_md(content.get("caption", []))
            lines.append(f"{prefix}*(image: {caption or 'embedded image'})*")
            lines.append("")

        elif btype in ("child_page", "child_database"):
            pass  # synced separately

        else:
            text = rich_text_to_md(content.get("rich_text", []))
            if text:
                lines.append(f"{prefix}{text}")
                lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------


def title_to_filename(title: str) -> str:
    cleaned = title.strip()
    # strip leading emoji
    while cleaned and not cleaned[0].isalnum() and cleaned[0] not in ("_",):
        cleaned = cleaned[1:].strip()
    name = cleaned.replace(" ", "_")
    if not name.upper().endswith(".MD"):
        name += ".md"
    else:
        name = name[:-3] + ".md"
    return name


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------


def sync_section(name: str, cfg: dict, headers: dict, dry_run: bool) -> list[str]:
    print(f"\n{'='*60}")
    print(f"Syncing: {name}")
    print(f"{'='*60}")

    target_dir = REPO_ROOT / cfg["dir"]
    target_dir.mkdir(exist_ok=True)

    child_pages = fetch_child_pages(cfg["page_id"], headers)
    written = []

    for page in child_pages:
        title = page["title"]
        filename = title_to_filename(title)
        filepath = target_dir / filename
        print(f"  {'[dry-run] ' if dry_run else ''}Fetching: {title} → {cfg['dir']}/{filename}")

        if dry_run:
            written.append(str(filepath.relative_to(REPO_ROOT)))
            continue

        blocks = fetch_blocks(page["id"], headers)
        md = blocks_to_markdown(blocks)
        filepath.write_text(f"# {title}\n\n{md}".rstrip() + "\n", encoding="utf-8")
        written.append(str(filepath.relative_to(REPO_ROOT)))

    return written


def sync_hub_main(headers: dict, dry_run: bool) -> str | None:
    if not HUB_PAGE_ID or HUB_PAGE_ID.startswith("YOUR_"):
        return None

    print(f"\n{'='*60}")
    print("Syncing: Hub main page")
    print(f"{'='*60}")

    filepath = REPO_ROOT / "COGNITIVE_HUB.md"
    if dry_run:
        print("  [dry-run] Would write: COGNITIVE_HUB.md")
        return str(filepath.relative_to(REPO_ROOT))

    blocks = fetch_blocks(HUB_PAGE_ID, headers)
    md = blocks_to_markdown(blocks)
    title = fetch_page_title(HUB_PAGE_ID, headers)
    filepath.write_text(f"# {title}\n\n{md}".rstrip() + "\n", encoding="utf-8")
    print("  Written: COGNITIVE_HUB.md")
    return str(filepath.relative_to(REPO_ROOT))


def git_commit_and_push():
    os.chdir(REPO_ROOT)
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    if not result.stdout.strip():
        print("\nNo changes to commit.")
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dirs = [cfg["dir"] for cfg in SECTIONS.values()]
    dirs.append("COGNITIVE_HUB.md")
    subprocess.run(["git", "add"] + dirs, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"sync: backup from Notion Hub ({timestamp})"],
        check=True,
    )
    print("\nCommitted. Pushing...")
    subprocess.run(["git", "push"], check=True)
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Sync Notion Cognitive Hub to local markdown")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    parser.add_argument("--commit", action="store_true", help="Git commit and push after sync")
    args = parser.parse_args()

    # Validate configuration
    for name, cfg in SECTIONS.items():
        if cfg["page_id"].startswith("YOUR_"):
            print(f"ERROR: Replace placeholder page_id for '{name}' in SECTIONS config.")
            sys.exit(1)

    token = get_token()
    headers = notion_headers(token)
    all_files = []

    hub_file = sync_hub_main(headers, args.dry_run)
    if hub_file:
        all_files.append(hub_file)

    for name, cfg in SECTIONS.items():
        all_files.extend(sync_section(name, cfg, headers, args.dry_run))

    print(f"\n{'='*60}")
    print(f"{'[DRY RUN] ' if args.dry_run else ''}Synced {len(all_files)} files")
    print(f"{'='*60}")

    if not args.dry_run and args.commit:
        git_commit_and_push()


if __name__ == "__main__":
    main()
