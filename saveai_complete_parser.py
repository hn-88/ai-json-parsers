#!/usr/bin/env python3
"""
saveai.net Complete Export Parser
Converts a directory (or list) of saveai.net export JSON files into an
indexed markdown archive, mirroring the structure produced by
claude_complete_parser.py.

BACKGROUND / FILE FORMAT
-------------------------
saveai.net is a browser extension that exports chat history from several
AI chat sites (AI Studio, ChatGPT, Claude, etc.) into one common schema.
Each exported file is a JSON array of individual MESSAGES (not
conversations) shaped like:

[
  {
    "id": "google_0",
    "chatGroupId": "1lgON64hDVrkUSXOv6muQLlASLKEakYqg",
    "role": "user",                       // "user" or "assistant"
    "model": "googleaistudio",            // short provider/site id
    "displayModel": "AI Studio",          // human-readable provider name
    "modelId": "models/gemini-3.5-flash-lite",  // present on assistant
                                                  // messages when known
    "contents": [
      { "type": "text", "content": "..." }
      // other content types (image, code, etc.) may appear depending on
      // plan/version; anything that isn't 'text' is rendered as a note
    ],
    "created_at": "",                     // often blank on the free tier
    "updated_at": 1789376188113           // epoch milliseconds
  },
  ...
]

Messages that belong to the same conversation share a "chatGroupId".
A single export file can contain messages from multiple chatGroupIds
(multiple conversations), and the same chatGroupId can in principle be
split across multiple files (e.g. incremental exports) - this script
merges messages by chatGroupId across every file it's given.

There is no title field inside the schema itself, but saveai.net names
each downloaded .json file after the conversation (or you've renamed it
yourself), so this script uses the source filename as the conversation
title. It only falls back to deriving a title from the first user
message if a chatGroupId somehow has no associated filename.

USAGE
-----
  python saveai_complete_parser.py /path/to/export_dir
  python saveai_complete_parser.py /path/to/export_dir -o my_archive
  python saveai_complete_parser.py export1.json export2.json
  python saveai_complete_parser.py /path/to/export_dir --no-recursive
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


class SaveAIParser:
    def __init__(self, paths: List[str], recursive: bool = True):
        self.input_paths = [Path(p) for p in paths]
        self.recursive = recursive

        # Raw messages collected from every file, keyed by chatGroupId -> list[message]
        self.groups: Dict[str, List[Dict]] = {}
        # Which source file(s) (by stem) each chatGroupId came from, keyed by chatGroupId -> set[str]
        self.group_source_files: Dict[str, set] = {}
        # After grouping/sorting: list of conversation records
        self.conversations: List[Dict] = []

        self._files_scanned = 0
        self._files_used = 0
        self._messages_total = 0

    # ------------------------------------------------------------------
    # Discovery / loading
    # ------------------------------------------------------------------

    def discover_files(self) -> List[Path]:
        files = []
        for p in self.input_paths:
            if p.is_dir():
                pattern = "**/*.json" if self.recursive else "*.json"
                files.extend(sorted(p.glob(pattern)))
            elif p.is_file():
                if p.suffix.lower() == '.json':
                    files.append(p)
                else:
                    print(f"Warning: skipping non-JSON file: {p}")
            else:
                print(f"Warning: path not found: {p} - skipping")

        seen = set()
        unique_files = []
        for f in files:
            resolved = f.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique_files.append(f)
        return unique_files

    def _extract_message_list(self, data) -> Optional[List[Dict]]:
        """saveai.net files are normally a bare JSON array of messages, but
        be forgiving in case a future export wraps it in an object."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ('messages', 'data', 'export', 'items'):
                if isinstance(data.get(key), list):
                    return data[key]
        return None

    def looks_like_saveai_message(self, item) -> bool:
        return isinstance(item, dict) and 'chatGroupId' in item and 'role' in item

    def load_data(self):
        candidate_files = self.discover_files()

        if not candidate_files:
            print("Error: No .json files found at the given path(s).")
            sys.exit(1)

        print(f"Found {len(candidate_files)} .json file(s) - checking format...")

        for file_path in candidate_files:
            self._files_scanned += 1
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except json.JSONDecodeError as e:
                print(f"  x JSON parse error in {file_path.name}: {e}")
                continue
            except Exception as e:
                print(f"  x Error reading {file_path.name}: {e}")
                continue

            messages = self._extract_message_list(data)
            if not messages or not any(self.looks_like_saveai_message(m) for m in messages):
                continue

            self._files_used += 1
            for msg in messages:
                if not self.looks_like_saveai_message(msg):
                    continue
                group_id = msg.get('chatGroupId', 'unknown_group')
                self.groups.setdefault(group_id, []).append(msg)
                self.group_source_files.setdefault(group_id, set()).add(file_path.stem)
                self._messages_total += 1

        print(f"\nSummary:")
        print(f"- Files scanned: {self._files_scanned}")
        print(f"- Files recognized as saveai.net exports: {self._files_used}")
        print(f"- Messages found: {self._messages_total}")
        print(f"- Conversations (chatGroupId groups): {len(self.groups)}")

        if not self.groups:
            print("\nError: No valid saveai.net export messages found!")
            print("Each message should contain 'chatGroupId' and 'role' fields.")
            sys.exit(1)

        self._build_conversations()

    def _id_trailing_number(self, msg: Dict) -> Optional[int]:
        m = re.search(r'(\d+)$', str(msg.get('id', '')))
        return int(m.group(1)) if m else None

    def _choose_sort_strategy(self, messages: List[Dict]) -> str:
        """Different sites' saveai.net exports carry ordering info
        differently, so pick whichever is actually trustworthy for this
        group rather than assuming one scheme fits everyone:

        - AI Studio's free-tier export gives every message in a group the
          SAME updated_at, but ids are sequential ('google_0', 'google_1',
          ...) - so the trailing id number is the only real ordering signal.
        - Claude/other exports give each message its own distinct
          updated_at, but ids are UUIDs where a trailing-digit sort is
          meaningless - so updated_at is the right signal there.

        Rule: if updated_at varies across the group, trust it. Otherwise
        fall back to the trailing id number (if every message has one).
        """
        timestamps = [m.get('updated_at') for m in messages if isinstance(m.get('updated_at'), (int, float))]
        if len(set(timestamps)) > 1:
            return 'timestamp'

        id_numbers = [self._id_trailing_number(m) for m in messages]
        if all(n is not None for n in id_numbers):
            return 'id_number'

        return 'timestamp'  # last resort, even if it won't distinguish anything

    def _build_conversations(self) -> None:
        for group_id, messages in self.groups.items():
            strategy = self._choose_sort_strategy(messages)
            if strategy == 'id_number':
                messages_sorted = sorted(messages, key=lambda m: self._id_trailing_number(m) or 0)
            else:
                messages_sorted = sorted(
                    messages,
                    key=lambda m: m.get('updated_at') if isinstance(m.get('updated_at'), (int, float)) else 0
                )

            timestamps = [m['updated_at'] for m in messages_sorted
                          if isinstance(m.get('updated_at'), (int, float))]
            first_ts = min(timestamps) if timestamps else None
            last_ts = max(timestamps) if timestamps else None

            display_model = next((m.get('displayModel') for m in messages_sorted if m.get('displayModel')), 'Unknown')
            model_ids = sorted({m.get('modelId') for m in messages_sorted if m.get('modelId')})

            source_files = sorted(self.group_source_files.get(group_id, []))

            self.conversations.append({
                'chat_group_id': group_id,
                'messages': messages_sorted,
                'display_model': display_model,
                'model_ids': model_ids,
                'first_ts': self._to_datetime(first_ts),
                'last_ts': self._to_datetime(last_ts),
                'source_files': source_files,
            })

    def _to_datetime(self, epoch_ms: Optional[float]) -> Optional[datetime]:
        if not epoch_ms:
            return None
        try:
            return datetime.fromtimestamp(epoch_ms / 1000)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def safe_filename(self, text: str, max_length: int = 100) -> str:
        filename = re.sub(r'[<>:"/\\|?*]', '_', text)
        filename = re.sub(r'[^\w\s\-_.]', '_', filename)
        filename = re.sub(r'\s+', '_', filename)
        filename = filename.strip('_')
        if len(filename) > max_length:
            filename = filename[:max_length].rstrip('_')
        return filename or "untitled"

    def format_date(self, dt: Optional[datetime]) -> str:
        if not dt:
            return "Unknown date"
        return dt.strftime("%B %d, %Y at %I:%M %p")

    # Content item types that hold plain renderable text (as opposed to
    # images, files, etc.). saveai.net uses "text" for most sites but
    # exports Claude conversations with "markdown" instead.
    TEXT_CONTENT_TYPES = {'text', 'markdown'}

    def extract_message_text(self, msg: Dict) -> str:
        """Join all text-like content items; note any other content types."""
        parts = []
        notes = []
        for item in msg.get('contents', []) or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get('type', 'text')
            if item_type in self.TEXT_CONTENT_TYPES:
                text = item.get('content', '')
                if text:
                    parts.append(text)
            else:
                notes.append(f"*[{item_type} content omitted]*")
        combined = '\n'.join(parts).strip()
        if notes:
            combined = (combined + '\n\n' + '\n'.join(notes)).strip()
        return combined

    def sender_label(self, msg: Dict) -> str:
        role = msg.get('role', 'unknown')
        return {'user': 'User', 'assistant': 'Assistant'}.get(role, role.title() if role else 'Unknown')

    def clean_summary_text(self, text: str, max_len: int = 80) -> str:
        clean = re.sub(r'```[\s\S]*?```', '[code block]', text)
        clean = re.sub(r'`[^`]*`', '[code]', clean)
        clean = re.sub(r'\n+', ' ', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        if len(clean) > max_len:
            return clean[:max_len].rstrip() + "..."
        return clean

    def first_user_text(self, conversation: Dict) -> str:
        for msg in conversation['messages']:
            if msg.get('role') == 'user':
                text = self.extract_message_text(msg)
                if text.strip():
                    return text.strip()
        return ""

    def get_conversation_summary(self, conversation: Dict) -> str:
        count = len(conversation['messages'])
        first = self.first_user_text(conversation)
        if first:
            return f"{count} messages - {self.clean_summary_text(first)}"
        return f"{count} messages"

    def display_title(self, conversation: Dict) -> str:
        """Title the conversation after its source .json filename (this is
        what saveai.net names the file as when you export/rename a chat),
        falling back to the first user message only if no filename is
        available for some reason."""
        source_files = conversation.get('source_files') or []
        if len(source_files) == 1:
            return source_files[0]
        if len(source_files) > 1:
            # Same chatGroupId split across multiple files (e.g. incremental
            # exports) - join their names so nothing is silently dropped.
            return " + ".join(source_files)

        first = self.first_user_text(conversation)
        if first:
            return self.clean_summary_text(first, max_len=50)
        short_id = conversation['chat_group_id'][-8:]
        return f"Untitled {conversation['display_model']} Conversation {short_id}"

    def has_meaningful_content(self, conversation: Dict) -> bool:
        return any(self.extract_message_text(m).strip() for m in conversation['messages'])

    # ------------------------------------------------------------------
    # Markdown generation
    # ------------------------------------------------------------------

    def create_conversation_file(self, conversation: Dict, output_dir: Path) -> str:
        title = self.display_title(conversation)
        filename = self.safe_filename(title) + '.md'

        header = f"# {title}\n\n## Conversation Details\n\n"

        model_line = f"**Model:** {conversation['display_model']}"
        if conversation['model_ids']:
            model_line += f" (`{'`, `'.join(conversation['model_ids'])}`)"

        meta = f"""{model_line}
**Chat Group ID:** `{conversation['chat_group_id']}`
**First Message:** {self.format_date(conversation['first_ts'])}
**Last Message:** {self.format_date(conversation['last_ts'])}

---

"""

        messages_section = self._create_messages_section(conversation['messages'])

        content = header + meta + messages_section

        conv_path = output_dir / filename
        counter = 2
        while conv_path.exists():
            conv_path = output_dir / f"{self.safe_filename(title)}_{counter}.md"
            counter += 1

        with open(conv_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return conv_path.name

    def _create_messages_section(self, messages: List[Dict]) -> str:
        if not messages:
            return "*No messages in this conversation.*\n"

        lines = [f"## Messages ({len(messages)})\n"]

        for i, msg in enumerate(messages, 1):
            sender = self.sender_label(msg)
            text = self.extract_message_text(msg)
            ts = self._to_datetime(msg.get('updated_at'))
            ts_str = f"\n*{self.format_date(ts)}*" if ts else ""

            block = f"### {i}. {sender}{ts_str}\n\n{text or '*[No content]*'}\n\n"
            lines.append(block)
            if i < len(messages):
                lines.append("---\n")

        return "".join(lines)

    def create_conversations_structure(self, output_dir: Path) -> None:
        conv_dir = output_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)

        meaningful = [c for c in self.conversations if self.has_meaningful_content(c)]
        skipped = len(self.conversations) - len(meaningful)

        meaningful.sort(key=lambda c: c['last_ts'] or datetime.min, reverse=True)

        for conversation in meaningful:
            filename = self.create_conversation_file(conversation, conv_dir)
            conversation['_md_filename'] = filename

        self.conversations = meaningful

        print(f"Created {len(meaningful)} conversation files")
        if skipped:
            print(f"Skipped {skipped} conversation(s) with no meaningful content")

    def create_readme(self, output_dir: Path) -> None:
        header = f"""# saveai.net Export - Complete Archive

Export generated on: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}

"""
        total_messages = sum(len(c['messages']) for c in self.conversations)
        providers_used = sorted({c['display_model'] for c in self.conversations})

        overview = f"""## Overview

- **Total Conversations:** {len(self.conversations)}
- **Total Messages:** {total_messages}
- **Providers/Sites:** {', '.join(providers_used) if providers_used else 'Unknown'}

"""

        dates = [c['last_ts'] for c in self.conversations if c['last_ts']]
        date_range = ""
        if dates:
            dates.sort()
            date_range = f"**Date Range:** {self.format_date(dates[0])} to {self.format_date(dates[-1])}\n\n"

        structure = f"""## Archive Structure

```
{output_dir.name}/
├── README.md (this file)
├── conversations/
│   └── [conversation_files].md
└── metadata/
    └── conversations_index.md
```

**Note:** saveai.net exports messages flat, grouped by `chatGroupId`.
Conversation titles above are taken from each source .json filename
(falling back to the first user message only if no filename is known).

"""

        recent = sorted(self.conversations, key=lambda c: c['last_ts'] or datetime.min, reverse=True)[:10]
        recent_lines = ["## Recent Conversations\n"]
        for c in recent:
            title = self.display_title(c)
            summary = self.get_conversation_summary(c)
            updated = self.format_date(c['last_ts'])
            fname = c.get('_md_filename', self.safe_filename(title) + '.md')
            recent_lines.append(f"- [{title}](conversations/{fname})\n  *{summary}* - {c['display_model']} - {updated}\n")
        recent_section = "".join(recent_lines) + "\n"

        nav = "## Navigation\n\n- [All Conversations](metadata/conversations_index.md)\n\n"

        content = header + overview + date_range + structure + recent_section + nav

        with open(output_dir / "README.md", 'w', encoding='utf-8') as f:
            f.write(content)

        print(f"Created main README: {output_dir / 'README.md'}")

    def create_metadata_files(self, output_dir: Path) -> None:
        metadata_dir = output_dir / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)

        total_messages = sum(len(c['messages']) for c in self.conversations)
        header = f"""# Conversations Index

Total conversations: **{len(self.conversations)}**
Total messages: **{total_messages}**

"""
        if not self.conversations:
            content = header + "No conversations found.\n"
        else:
            sorted_convs = sorted(self.conversations, key=lambda c: c['last_ts'] or datetime.min, reverse=True)
            lines = ["## All Conversations\n\n"]
            for c in sorted_convs:
                title = self.display_title(c)
                updated = self.format_date(c['last_ts'])
                summary = self.get_conversation_summary(c)
                fname = c.get('_md_filename', self.safe_filename(title) + '.md')
                lines.append(
                    f"- [{title}](../conversations/{fname})\n"
                    f"  *{summary}* - {c['display_model']} - Updated {updated}\n\n"
                )
            content = header + "".join(lines)

        with open(metadata_dir / "conversations_index.md", 'w', encoding='utf-8') as f:
            f.write(content)

        print("Created metadata files")

    def export_to_markdown(self, output_dir: str) -> None:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        print(f"\nExporting saveai.net archive to: {output_path}")
        print("=" * 60)

        self.create_conversations_structure(output_path)
        self.create_readme(output_path)
        self.create_metadata_files(output_path)

        print("=" * 60)
        print(f"Export complete! Archive created in: {output_path}")
        print(f"Start by opening: {output_path / 'README.md'}")

    def print_summary(self) -> None:
        total_messages = sum(len(c['messages']) for c in self.conversations)
        print("\n" + "=" * 60)
        print("SAVEAI.NET EXPORT SUMMARY")
        print("=" * 60)
        print(f"Conversations: {len(self.conversations)}")
        print(f"  - Total messages: {total_messages}")
        providers = sorted({c['display_model'] for c in self.conversations})
        print(f"  - Providers/sites: {', '.join(providers) if providers else 'Unknown'}")
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description='Convert a directory of saveai.net export JSON files into an indexed markdown archive',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Point at a directory containing many saveai.net .json exports
  python saveai_complete_parser.py ./saveai_exports

  # Non-recursive (top-level files only)
  python saveai_complete_parser.py ./saveai_exports --no-recursive

  # Or list individual files
  python saveai_complete_parser.py export1.json export2.json

  # Custom output directory
  python saveai_complete_parser.py ./saveai_exports -o my_archive

Note: Files are auto-detected by content (each message must have
'chatGroupId' and 'role' fields) rather than by filename. Messages
sharing a chatGroupId - even across different files - are merged into
one conversation.
        """
    )

    parser.add_argument('paths', nargs='+', help='Directory of saveai.net export JSON files, and/or individual JSON files')
    parser.add_argument('-o', '--output', default='saveai_complete_export',
                         help='Output directory for markdown files (default: saveai_complete_export)')
    parser.add_argument('--no-recursive', action='store_true',
                         help="Don't search subdirectories when a directory is given")

    args = parser.parse_args()

    print("saveai.net Complete Export Parser")
    print("=" * 50)

    parser_instance = SaveAIParser(args.paths, recursive=not args.no_recursive)
    parser_instance.load_data()
    parser_instance.print_summary()
    parser_instance.export_to_markdown(args.output)


if __name__ == "__main__":
    main()
