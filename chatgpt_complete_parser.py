#!/usr/bin/env python3
"""
ChatGPT Complete Export Parser
Converts an OpenAI data-export (conversations-*.json, user.json) into a
unified markdown archive, mirroring the structure/conventions of the
Claude Complete Export Parser.

ChatGPT's export stores each conversation as a tree in `mapping`
(node id -> {id, message, parent}), with `current_node` pointing at the
active leaf. This parser walks from `current_node` back to the root to
reconstruct exactly the thread that was visible in the ChatGPT UI /
chat.html (skipping abandoned "regenerate" branches), then reverses it
into chronological order. Each message's own `create_time` is used for
per-turn timestamps, and the conversation's `create_time`/`update_time`
give the date/time info that's missing from chat.html.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


ROLE_LABEL = {"user": "Human", "assistant": "ChatGPT", "tool": "Tool"}


class ChatGPTCompleteParser:
    def __init__(self, file_paths: List[str]):
        self.file_paths = [Path(p) for p in file_paths]

        self.conversations: List[Dict] = []
        self.users: List[Dict] = []

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    def detect_file_type(self, file_path: Path) -> str:
        """Detect the type of ChatGPT export file by analyzing filename"""
        filename = file_path.name.lower()

        if 'conversation' in filename:
            return "conversations"
        elif filename in ('user.json',) or (filename.startswith('user') and 'settings' not in filename):
            return "users"
        else:
            return "unknown"

    def load_data(self):
        """Load and parse JSON files with automatic filename-based type detection"""
        conversations_found = False

        for file_path in self.file_paths:
            if not file_path.exists():
                print(f"Warning: File not found: {file_path} - skipping")
                continue

            file_type = self.detect_file_type(file_path)

            try:
                print(f"Loading {file_path}...")
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                if file_type == "conversations":
                    if isinstance(data, dict):
                        data = [data]
                    self.conversations.extend(data)
                    conversations_found = True
                    print(f"✓ Detected conversations file: {len(data)} conversations")

                elif file_type == "users":
                    if isinstance(data, dict):
                        data = [data]
                    self.users.extend(data)
                    print(f"✓ Detected user file: {len(data)} user record(s)")

                else:
                    print(
                        f"? Unknown file type: {file_path} (filename doesn't contain "
                        f"'conversation' or 'user') - skipping")

            except json.JSONDecodeError as e:
                print(f"✗ JSON parsing error in {file_path}: {e}")
                continue
            except Exception as e:
                print(f"✗ Error loading {file_path}: {e}")
                continue

        if not conversations_found:
            print("\nError: No conversations file found!")
            print("Make sure at least one filename contains 'conversation' "
                  "(e.g., conversations-000.json)")
            sys.exit(1)

        print(f"\nSummary:")
        print(f"- Conversations: {len(self.conversations)}")
        print(f"- Users: {len(self.users)}")

    # ------------------------------------------------------------------ #
    # Formatting helpers
    # ------------------------------------------------------------------ #

    def format_date(self, ts) -> str:
        """Format a unix epoch timestamp (float/int, as ChatGPT stores it) to
        a readable string. Returns 'Unknown date' if missing/unparsable."""
        if ts is None or ts == '':
            return "Unknown date"
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            return dt.strftime("%B %d, %Y at %I:%M %p UTC")
        except (ValueError, OSError, OverflowError, TypeError):
            return "Unknown date"

    def sort_key_date(self, ts) -> float:
        """Sortable numeric key for a timestamp, missing values sort last."""
        try:
            return float(ts)
        except (TypeError, ValueError):
            return float('-inf')

    def safe_filename(self, text: str, max_length: int = 100) -> str:
        """Convert text to safe filename"""
        filename = re.sub(r'[<>:"/\\|?*]', '_', text or '')
        filename = re.sub(r'[^\w\s\-_.]', '_', filename)
        filename = re.sub(r'\s+', '_', filename)
        filename = filename.strip('_').rstrip('.')

        if len(filename) > max_length:
            filename = filename[:max_length].rstrip('_')

        return filename or "untitled"

    # ------------------------------------------------------------------ #
    # Tree walking / message extraction
    # ------------------------------------------------------------------ #

    def part_to_text(self, part) -> Optional[str]:
        """Turn a single 'parts' entry into markdown text. Parts are usually
        strings but can be dicts (image/audio asset pointers, tool payloads)."""
        if isinstance(part, str):
            return part
        if isinstance(part, dict):
            ctype = part.get("content_type")
            if ctype in ("image_asset_pointer", "audio_asset_pointer",
                         "video_container_asset_pointer"):
                return f"*[{ctype.replace('_', ' ')} attachment omitted]*"
            for key in ("text", "value"):
                if key in part and isinstance(part[key], str):
                    return part[key]
            return f"*[non-text content: {ctype or 'unknown'}]*"
        return None

    def format_message_content(self, message: Dict) -> str:
        """Extract markdown text from a single message dict."""
        if not message:
            return ""
        content = message.get("content") or {}
        ctype = content.get("content_type")

        if ctype in ("text", "multimodal_text"):
            parts = content.get("parts") or []
            rendered = [self.part_to_text(p) for p in parts]
            rendered = [r for r in rendered if r and r.strip()]
            return "\n\n".join(rendered).strip()

        if ctype == "code":
            lang = content.get("language", "")
            text = content.get("text", "")
            return f"```{lang}\n{text}\n```"

        if ctype == "execution_output":
            text = content.get("text", "")
            return f"**Execution output:**\n```\n{text}\n```"

        # tether_browsing_display, tether_quote, and other tool-plumbing
        # content types are intentionally not surfaced in the transcript.
        return ""

    def should_include(self, message: Optional[Dict]) -> bool:
        if not message:
            return False
        author = message.get("author") or {}
        role = author.get("role")
        metadata = message.get("metadata") or {}

        if metadata.get("is_visually_hidden_from_conversation"):
            return False
        if role not in ("user", "assistant"):
            # system / tool messages are hidden plumbing, not visible turns
            return False
        return True

    def build_thread(self, conversation: Dict) -> List[Dict]:
        """Walk from the active leaf (current_node) back to the root via
        parent pointers, then reverse into chronological order. This
        follows the branch actually left active, matching chat.html."""
        mapping = conversation.get("mapping") or {}
        current_node = conversation.get("current_node")

        thread = []
        node_id = current_node
        visited = set()
        while node_id and node_id in mapping and node_id not in visited:
            visited.add(node_id)
            node = mapping[node_id]
            msg = node.get("message")
            if self.should_include(msg):
                thread.append(msg)
            node_id = node.get("parent")
        thread.reverse()
        return thread

    def get_messages(self, conversation: Dict) -> List[Dict]:
        """Cache-friendly accessor: returns the reconstructed thread."""
        return self.build_thread(conversation)

    def has_meaningful_content(self, conversation: Dict) -> bool:
        for msg in self.get_messages(conversation):
            if self.format_message_content(msg).strip():
                return True
        return False

    def get_conversation_summary(self, conversation: Dict) -> str:
        """Generate a brief summary of the conversation"""
        messages = self.get_messages(conversation)
        if not messages:
            return "No messages"

        message_count = len(messages)
        first_message = None

        for msg in messages:
            if (msg.get('author') or {}).get('role') == 'user':
                first_message = self.format_message_content(msg)
                if first_message:
                    break

        if first_message:
            clean_message = re.sub(r'```[\s\S]*?```', '[code block]', first_message)
            clean_message = re.sub(r'`[^`]*`', '[code]', clean_message)
            clean_message = re.sub(r'\n+', ' ', clean_message)
            clean_message = re.sub(r'\s+', ' ', clean_message)
            clean_message = clean_message.strip()

            if len(clean_message) > 80:
                summary = clean_message[:80].rstrip() + "..."
            else:
                summary = clean_message

            return f"{message_count} messages - {summary}"

        return f"{message_count} messages"

    def get_conversation_display_name(self, conversation: Dict) -> str:
        """Get a proper display name for a conversation, handling empty titles"""
        name = (conversation.get('title') or '').strip()

        if not name:
            for msg in self.get_messages(conversation):
                if (msg.get('author') or {}).get('role') == 'user':
                    first_message = self.format_message_content(msg)
                    if first_message:
                        clean_message = re.sub(r'```[\s\S]*?```', '[code block]', first_message)
                        clean_message = re.sub(r'`[^`]*`', '[code]', clean_message)
                        clean_message = re.sub(r'\n+', ' ', clean_message)
                        clean_message = re.sub(r'\s+', ' ', clean_message)
                        clean_message = clean_message.strip()

                        name = clean_message[:50].rstrip() + "..." if len(clean_message) > 50 else clean_message
                        break

        if not name:
            conv_id = conversation.get('conversation_id') or conversation.get('id') or 'unknown'
            short_id = conv_id[-8:] if len(conv_id) >= 8 else conv_id
            name = f"Untitled Conversation {short_id}"

        return name

    # ------------------------------------------------------------------ #
    # README
    # ------------------------------------------------------------------ #

    def create_readme(self, output_dir: Path) -> None:
        header_content = f"""# ChatGPT Export - Complete Archive

Export generated on: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}

"""

        user_info_section = ""
        if self.users:
            user = self.users[0]
            user_id = user.get('id') or user.get('user_id') or 'Unknown'
            user_info_section = f"""## User Information

**Email:** {user.get('email', 'Unknown')}
**User ID:** `{user_id}`

"""

        total_messages = sum(len(self.get_messages(conv)) for conv in self.conversations)

        overview_section = f"""## Overview

- **Total Conversations:** {len(self.conversations)}
- **Total Messages:** {total_messages}

"""

        date_range_section = ""
        dates = [conv.get('create_time') for conv in self.conversations if conv.get('create_time')]
        if dates:
            dates.sort()
            first_date = self.format_date(dates[0])
            last_date = self.format_date(dates[-1])
            date_range_section = f"""**Date Range:** {first_date} to {last_date}

"""

        structure_section = f"""## Archive Structure

This archive is organized as follows:

```
{output_dir.name}/
├── README.md (this file)
├── conversations/
│   └── standalone_conversations/
│       └── [conversation_files]
└── metadata/
    ├── conversations_index.md
    └── user_info.md
```

"""

        recent_conversations_section = self._create_recent_conversations_section()

        navigation_section = """## Navigation

- [All Conversations](metadata/conversations_index.md)
- [User Information](metadata/user_info.md)

"""

        readme_content = (
                header_content +
                user_info_section +
                overview_section +
                date_range_section +
                structure_section +
                recent_conversations_section +
                navigation_section
        )

        readme_path = output_dir / "README.md"
        with open(readme_path, 'w', encoding='utf-8') as f:
            f.write(readme_content)

        print(f"Created main README: {readme_path}")

    def _create_recent_conversations_section(self) -> str:
        section_lines = ["## Recent Conversations\n"]

        recent_conversations = sorted(
            self.conversations,
            key=lambda x: self.sort_key_date(x.get('update_time')),
            reverse=True
        )[:10]

        for conv in recent_conversations:
            name = self.get_conversation_display_name(conv)
            summary = self.get_conversation_summary(conv)
            updated = self.format_date(conv.get('update_time'))
            filename = self.safe_filename(name) + '.md'

            conversation_line = (
                f"- [{name}](conversations/standalone_conversations/{filename})\n"
                f"  *{summary}* - {updated}\n"
            )
            section_lines.append(conversation_line)

        section_lines.append("\n")
        return "".join(section_lines)

    # ------------------------------------------------------------------ #
    # Conversation files
    # ------------------------------------------------------------------ #

    def create_conversations_structure(self, output_dir: Path) -> None:
        conversations_dir = output_dir / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)

        standalone_dir = conversations_dir / "standalone_conversations"
        standalone_dir.mkdir(parents=True, exist_ok=True)

        meaningful_conversations = [c for c in self.conversations if self.has_meaningful_content(c)]
        skipped_count = len(self.conversations) - len(meaningful_conversations)

        used_names: Dict[str, int] = {}
        for conversation in meaningful_conversations:
            self.create_conversation_file(conversation, standalone_dir, used_names)

        print(f"Created {len(meaningful_conversations)} conversation files")
        if skipped_count > 0:
            print(f"Skipped {skipped_count} conversations with no meaningful content")

        self.conversations = meaningful_conversations

    def create_conversation_file(self, conversation: Dict, output_dir: Path,
                                  used_names: Dict[str, int]) -> None:
        name = self.get_conversation_display_name(conversation)
        base = self.safe_filename(name)
        n = used_names.get(base, 0)
        used_names[base] = n + 1
        filename = f"{base}.md" if n == 0 else f"{base}_{n}.md"

        header_section = f"""# {name}

## Conversation Details

"""

        conv_id = conversation.get('conversation_id') or conversation.get('id') or 'Unknown'
        created_line = f"**Created:** {self.format_date(conversation.get('create_time'))}"
        updated_line = f"**Last Updated:** {self.format_date(conversation.get('update_time'))}"
        conv_id_line = f"**Conversation ID:** `{conv_id}`"

        model = conversation.get('default_model_slug')
        model_line = f"\n**Model:** `{model}`" if model else ""

        metadata_section = created_line + "\n" + updated_line + "\n" + conv_id_line + model_line + "\n\n---\n\n"

        messages_section = self._create_messages_section(self.get_messages(conversation))

        content = header_section + metadata_section + messages_section

        conv_path = output_dir / filename
        with open(conv_path, 'w', encoding='utf-8') as f:
            f.write(content)

    def _create_messages_section(self, messages: List[Dict]) -> str:
        if not messages:
            return "*No messages in this conversation.*"

        section_lines = [f"## Messages ({len(messages)})\n"]

        for i, message in enumerate(messages, 1):
            role = (message.get('author') or {}).get('role', 'unknown')
            timestamp = self.format_date(message.get('create_time'))
            content = self.format_message_content(message)

            sender_name = ROLE_LABEL.get(role, role.title())

            message_block = f"""### {i}. {sender_name}
*{timestamp}*

{content or '*[No content]*'}

"""
            section_lines.append(message_block)

            if i < len(messages):
                section_lines.append("---\n")

        return "".join(section_lines)

    # ------------------------------------------------------------------ #
    # Metadata / index files
    # ------------------------------------------------------------------ #

    def create_metadata_files(self, output_dir: Path) -> None:
        metadata_dir = output_dir / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)

        self.create_conversations_index(metadata_dir)
        self.create_user_info(metadata_dir)

        print("Created metadata files")

    def create_conversations_index(self, metadata_dir: Path) -> None:
        total_messages = sum(len(self.get_messages(conv)) for conv in self.conversations)

        header_section = f"""# Conversations Index

Total conversations: **{len(self.conversations)}**
Total messages: **{total_messages}**

"""

        if not self.conversations:
            content = header_section + "No conversations found.\n"
        else:
            sorted_conversations = sorted(
                self.conversations,
                key=lambda x: self.sort_key_date(x.get('update_time')),
                reverse=True
            )

            conversations_section = "## All Conversations\n\n"
            used_names: Dict[str, int] = {}

            for conversation in sorted_conversations:
                name = self.get_conversation_display_name(conversation)
                updated = self.format_date(conversation.get('update_time'))
                summary = self.get_conversation_summary(conversation)

                base = self.safe_filename(name)
                n = used_names.get(base, 0)
                used_names[base] = n + 1
                filename = f"{base}.md" if n == 0 else f"{base}_{n}.md"

                conversation_line = (
                    f"- [{name}](../conversations/standalone_conversations/{filename})\n"
                    f"  *{summary}* - Updated {updated}\n\n"
                )
                conversations_section += conversation_line

            content = header_section + conversations_section

        index_path = metadata_dir / "conversations_index.md"
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(content)

    def create_user_info(self, metadata_dir: Path) -> None:
        content = "# User Information\n\n"

        if not self.users:
            content += "No user information available."
        else:
            for user in self.users:
                user_id = user.get('id') or user.get('user_id') or 'Unknown'
                user_block = f"""**Email:** {user.get('email', 'Unknown')}
**User ID:** `{user_id}`
**ChatGPT Plus:** {user.get('chatgpt_plus_user', 'Unknown')}

"""
                content += user_block

        user_path = metadata_dir / "user_info.md"
        with open(user_path, 'w', encoding='utf-8') as f:
            f.write(content)

    # ------------------------------------------------------------------ #
    # Top-level export / summary
    # ------------------------------------------------------------------ #

    def export_to_markdown(self, output_dir: str) -> None:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        print(f"\nExporting complete ChatGPT archive to: {output_path}")
        print("=" * 60)

        self.create_readme(output_path)
        self.create_conversations_structure(output_path)
        # Recreate README's recent-conversations section now that
        # has_meaningful_content filtering has pruned self.conversations,
        # and filenames/dedup match what's actually on disk.
        self.create_readme(output_path)
        self.create_metadata_files(output_path)

        print("=" * 60)
        print(f"Export complete! Archive created in: {output_path}")
        print(f"Start by opening: {output_path / 'README.md'}")

    def print_summary(self) -> None:
        total_messages = sum(len(self.get_messages(conv)) for conv in self.conversations)

        print("\n" + "=" * 60)
        print("CHATGPT EXPORT SUMMARY")
        print("=" * 60)
        print(f"Users: {len(self.users)}")
        print(f"Conversations: {len(self.conversations)}")
        print(f"  - Total messages: {total_messages}")
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description='Convert ChatGPT export files to a Claude-parser-style markdown archive '
                    '(auto-detects file types)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # All numbered conversation files plus user.json
  python chatgpt_complete_parser.py conversations-*.json user.json

  # Just conversations
  python chatgpt_complete_parser.py conversations-000.json conversations-001.json

  # Custom output directory
  python chatgpt_complete_parser.py conversations-*.json user.json -o my_archive

Note: Files are identified by filename (must contain 'conversation', or be user.json)
      Multiple conversations-*.json files are merged into a single archive.
        """
    )

    parser.add_argument('files', nargs='+', help='ChatGPT export JSON files (auto-detected by filename)')
    parser.add_argument(
        '-o', '--output',
        default='chatgpt_complete_export',
        help='Output directory for markdown files (default: chatgpt_complete_export)'
    )

    args = parser.parse_args()

    print("ChatGPT Complete Export Parser (Smart Detection)")
    print("=" * 50)

    parser_instance = ChatGPTCompleteParser(args.files)

    parser_instance.load_data()
    parser_instance.print_summary()
    parser_instance.export_to_markdown(args.output)


if __name__ == "__main__":
    main()
