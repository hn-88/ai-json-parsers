#!/usr/bin/env python3
"""
AI Studio Complete Export Parser
Converts a directory (or list) of Google AI Studio chat export JSON files
into an indexed markdown archive, mirroring the structure produced by
claude_complete_parser.py.

BACKGROUND / FILE FORMAT
-------------------------
Unlike Claude's export (one conversations.json containing every chat),
Google AI Studio has no bulk "export all chats" feature. Each chat is
saved separately, either via the in-app "Download" action or via
Drive auto-save, as its own .json file shaped like:

{
  "runSettings": {
    "temperature": 1.0,
    "model": "models/gemini-2.5-pro",
    "topP": 0.95,
    "topK": 40,
    "maxOutputTokens": 8192,
    "safetySettings": [...]
  },
  "systemInstruction": { "parts": [{ "text": "..." }] },   // or {} if unset
  "citations": [ { "uri": "..." }, ... ],                  // optional
  "chunkedPrompt": {
    "chunks": [
      { "text": "...", "role": "user", "tokenCount": 12 },
      { "text": "...", "role": "model", "isThought": true, "tokenCount": 40 },
      { "text": "...", "role": "model", "finishReason": "STOP", "tokenCount": 90 }
    ],
    "pendingInputs": [ { "text": "", "role": "user" } ]     // optional, unsent draft
  }
}

There is no title or timestamp field in the native file. This script uses
the filename (minus extension) as the conversation title, and the file's
filesystem modified-time as a fallback "last updated" timestamp (AI Studio
filenames often already contain a readable title if you renamed the chat
before downloading/auto-saving).

Because each file is a single conversation, this script simply walks every
.json file in a directory (recursively, by default) rather than needing
Claude-style filename type detection.

USAGE
-----
  python aistudio_complete_parser.py /path/to/export_dir
  python aistudio_complete_parser.py /path/to/export_dir -o my_archive
  python aistudio_complete_parser.py chat1.json chat2.json chat3.json
  python aistudio_complete_parser.py /path/to/export_dir --no-thoughts
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


class AIStudioParser:
    def __init__(self, paths: List[str], recursive: bool = True, include_thoughts: bool = True):
        self.input_paths = [Path(p) for p in paths]
        self.recursive = recursive
        self.include_thoughts = include_thoughts

        # Each entry: dict with keys: path, title, model, run_settings,
        # system_instruction, citations, chunks, pending_inputs, mtime, error
        self.conversations: List[Dict] = []

    # ------------------------------------------------------------------
    # Discovery / loading
    # ------------------------------------------------------------------

    def discover_files(self) -> List[Path]:
        """Expand input paths (dirs or files) into a flat list of .json files"""
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

        # De-duplicate while preserving order
        seen = set()
        unique_files = []
        for f in files:
            resolved = f.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique_files.append(f)
        return unique_files

    def looks_like_aistudio_chat(self, data) -> bool:
        """Heuristic check that a JSON file is an AI Studio chat export"""
        return isinstance(data, dict) and 'chunkedPrompt' in data

    def load_data(self):
        """Load every JSON file, keeping only ones that look like AI Studio chats"""
        candidate_files = self.discover_files()

        if not candidate_files:
            print("Error: No .json files found at the given path(s).")
            sys.exit(1)

        print(f"Found {len(candidate_files)} .json file(s) - checking format...")

        skipped_not_chat = 0
        skipped_error = 0

        for file_path in candidate_files:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except json.JSONDecodeError as e:
                print(f"  x JSON parse error in {file_path.name}: {e}")
                skipped_error += 1
                continue
            except Exception as e:
                print(f"  x Error reading {file_path.name}: {e}")
                skipped_error += 1
                continue

            if not self.looks_like_aistudio_chat(data):
                skipped_not_chat += 1
                continue

            self.conversations.append(self._parse_conversation(file_path, data))

        print(f"\nSummary:")
        print(f"- AI Studio chat files parsed: {len(self.conversations)}")
        if skipped_not_chat:
            print(f"- Skipped (no 'chunkedPrompt' key, not an AI Studio chat): {skipped_not_chat}")
        if skipped_error:
            print(f"- Skipped (read/parse errors): {skipped_error}")

        if not self.conversations:
            print("\nError: No valid AI Studio chat files found!")
            print("Each AI Studio export file should be a JSON object containing a 'chunkedPrompt' key.")
            sys.exit(1)

    def _parse_conversation(self, file_path: Path, data: Dict) -> Dict:
        """Normalize a single AI Studio chat file into an internal record"""
        run_settings = data.get('runSettings', {}) or {}
        system_instruction = self._extract_system_instruction(data.get('systemInstruction', {}))
        citations = data.get('citations', []) or []

        chunked_prompt = data.get('chunkedPrompt', {}) or {}
        chunks = chunked_prompt.get('chunks', []) or []
        pending_inputs = chunked_prompt.get('pendingInputs', []) or []

        try:
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
        except Exception:
            mtime = None

        return {
            'path': file_path,
            'title': file_path.stem,
            'model': run_settings.get('model', 'Unknown model'),
            'run_settings': run_settings,
            'system_instruction': system_instruction,
            'citations': citations,
            'chunks': chunks,
            'pending_inputs': pending_inputs,
            'mtime': mtime,
        }

    def _extract_system_instruction(self, system_instruction) -> str:
        """systemInstruction can be {}, {"text": "..."} or {"parts": [{"text": "..."}]}"""
        if not system_instruction:
            return ""
        if isinstance(system_instruction, str):
            return system_instruction.strip()
        if 'text' in system_instruction:
            return str(system_instruction.get('text', '')).strip()
        parts = system_instruction.get('parts', [])
        texts = [p.get('text', '') for p in parts if isinstance(p, dict) and p.get('text')]
        return '\n'.join(texts).strip()

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

    def sender_label(self, chunk: Dict) -> str:
        role = chunk.get('role', 'unknown')
        if role == 'user':
            return 'User'
        if role == 'model':
            return 'Model (thinking)' if chunk.get('isThought') else 'Model'
        return role.title() if role else 'Unknown'

    def visible_chunks(self, conversation: Dict) -> List[Dict]:
        chunks = conversation['chunks']
        if self.include_thoughts:
            return chunks
        return [c for c in chunks if not c.get('isThought')]

    def message_count(self, conversation: Dict) -> int:
        return len(self.visible_chunks(conversation))

    def first_user_text(self, conversation: Dict) -> str:
        for chunk in conversation['chunks']:
            if chunk.get('role') == 'user' and chunk.get('text', '').strip():
                return chunk['text'].strip()
        return ""

    def clean_summary_text(self, text: str, max_len: int = 80) -> str:
        clean = re.sub(r'```[\s\S]*?```', '[code block]', text)
        clean = re.sub(r'`[^`]*`', '[code]', clean)
        clean = re.sub(r'\n+', ' ', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        if len(clean) > max_len:
            return clean[:max_len].rstrip() + "..."
        return clean

    def get_conversation_summary(self, conversation: Dict) -> str:
        count = self.message_count(conversation)
        first = self.first_user_text(conversation)
        if first:
            return f"{count} messages - {self.clean_summary_text(first)}"
        return f"{count} messages"

    def display_title(self, conversation: Dict) -> str:
        title = conversation['title'].strip()
        # Filenames like "AI Studio 2026-01-15" or hash-like names aren't very
        # readable; if the title looks generic/empty, fall back to first message
        if not title or title.lower() in ('untitled', 'chat', 'conversation'):
            first = self.first_user_text(conversation)
            if first:
                title = self.clean_summary_text(first, max_len=50)
            else:
                title = "Untitled AI Studio Chat"
        return title

    def has_meaningful_content(self, conversation: Dict) -> bool:
        return any(c.get('text', '').strip() for c in conversation['chunks'])

    # ------------------------------------------------------------------
    # Markdown generation
    # ------------------------------------------------------------------

    def create_conversation_file(self, conversation: Dict, output_dir: Path) -> str:
        """Write one conversation to markdown, return the filename used"""
        title = self.display_title(conversation)
        filename = self.safe_filename(title) + '.md'

        header = f"# {title}\n\n## Conversation Details\n\n"

        model = conversation['model']
        rs = conversation['run_settings']
        settings_bits = []
        for key, label in [
            ('temperature', 'Temperature'), ('topP', 'Top P'), ('topK', 'Top K'),
            ('maxOutputTokens', 'Max Output Tokens'), ('responseMimeType', 'Response MIME Type'),
        ]:
            if key in rs:
                settings_bits.append(f"**{label}:** {rs[key]}")
        settings_line = "  \n".join(settings_bits)

        meta = f"""**Model:** `{model}`
**Source File:** `{conversation['path'].name}`
**Last Modified:** {self.format_date(conversation['mtime'])}
{settings_line}

"""

        # System instruction
        sys_section = ""
        if conversation['system_instruction']:
            sys_section = f"## System Instruction\n\n> {conversation['system_instruction']}\n\n"

        # Citations
        cit_section = ""
        citations = conversation['citations']
        if citations:
            lines = ["## Citations\n"]
            for c in citations:
                uri = c.get('uri', '') if isinstance(c, dict) else str(c)
                if uri:
                    lines.append(f"- {uri}")
            cit_section = "\n".join(lines) + "\n\n"

        meta_section = meta + sys_section + cit_section + "---\n\n"

        # Messages
        messages_section = self._create_messages_section(conversation)

        # Pending / unsent draft
        pending_section = ""
        pending = [p for p in conversation['pending_inputs'] if p.get('text', '').strip()]
        if pending:
            lines = ["\n---\n\n## Unsent Draft (pending input, never submitted)\n"]
            for p in pending:
                lines.append(f"\n{p.get('text', '').strip()}\n")
            pending_section = "".join(lines)

        content = header + meta_section + messages_section + pending_section

        conv_path = output_dir / filename
        # Avoid collisions between conversations that share a display title
        counter = 2
        while conv_path.exists():
            conv_path = output_dir / f"{self.safe_filename(title)}_{counter}.md"
            counter += 1

        with open(conv_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return conv_path.name

    def _create_messages_section(self, conversation: Dict) -> str:
        chunks = self.visible_chunks(conversation)
        if not chunks:
            return "*No messages in this conversation.*\n"

        lines = [f"## Messages ({len(chunks)})\n"]

        for i, chunk in enumerate(chunks, 1):
            sender = self.sender_label(chunk)
            text = chunk.get('text', '').strip()
            token_count = chunk.get('tokenCount')
            finish_reason = chunk.get('finishReason')

            footer_bits = []
            if token_count is not None:
                footer_bits.append(f"{token_count} tokens")
            if finish_reason and finish_reason != 'STOP':
                footer_bits.append(f"finish reason: {finish_reason}")
            footer = f"\n\n*({', '.join(footer_bits)})*" if footer_bits else ""

            block = f"### {i}. {sender}\n\n{text or '*[No content]*'}{footer}\n\n"
            lines.append(block)
            if i < len(chunks):
                lines.append("---\n")

        return "".join(lines)

    def create_conversations_structure(self, output_dir: Path) -> None:
        conv_dir = output_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)

        meaningful = [c for c in self.conversations if self.has_meaningful_content(c)]
        skipped = len(self.conversations) - len(meaningful)

        # Sort newest-first by file mtime for stable, predictable ordering
        meaningful.sort(key=lambda c: c['mtime'] or datetime.min, reverse=True)

        for conversation in meaningful:
            filename = self.create_conversation_file(conversation, conv_dir)
            conversation['_md_filename'] = filename

        self.conversations = meaningful

        print(f"Created {len(meaningful)} conversation files")
        if skipped:
            print(f"Skipped {skipped} conversation(s) with no meaningful content")

    def create_readme(self, output_dir: Path) -> None:
        header = f"""# AI Studio Export - Complete Archive

Export generated on: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}

"""
        total_messages = sum(self.message_count(c) for c in self.conversations)
        models_used = sorted({c['model'] for c in self.conversations})

        overview = f"""## Overview

- **Total Conversations:** {len(self.conversations)}
- **Total Messages:** {total_messages}
- **Models Used:** {', '.join(f'`{m}`' for m in models_used) if models_used else 'Unknown'}

"""

        dates = [c['mtime'] for c in self.conversations if c['mtime']]
        date_range = ""
        if dates:
            dates.sort()
            date_range = f"**Date Range (by file modified time):** {self.format_date(dates[0])} to {self.format_date(dates[-1])}\n\n"

        structure = f"""## Archive Structure

```
{output_dir.name}/
├── README.md (this file)
├── conversations/
│   └── [conversation_files].md
└── metadata/
    └── conversations_index.md
```

**Note:** Native AI Studio exports don't include a conversation timestamp,
so ordering and "Last Modified" above use each source file's filesystem
modified time. Rename source files before running this script if you want
more meaningful titles than the ones AI Studio (or Drive auto-save) gave them.

"""

        recent = sorted(self.conversations, key=lambda c: c['mtime'] or datetime.min, reverse=True)[:10]
        recent_lines = ["## Recent Conversations\n"]
        for c in recent:
            title = self.display_title(c)
            summary = self.get_conversation_summary(c)
            updated = self.format_date(c['mtime'])
            fname = c.get('_md_filename', self.safe_filename(title) + '.md')
            recent_lines.append(f"- [{title}](conversations/{fname})\n  *{summary}* - {updated}\n")
        recent_section = "".join(recent_lines) + "\n"

        nav = "## Navigation\n\n- [All Conversations](metadata/conversations_index.md)\n\n"

        content = header + overview + date_range + structure + recent_section + nav

        with open(output_dir / "README.md", 'w', encoding='utf-8') as f:
            f.write(content)

        print(f"Created main README: {output_dir / 'README.md'}")

    def create_metadata_files(self, output_dir: Path) -> None:
        metadata_dir = output_dir / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)

        total_messages = sum(self.message_count(c) for c in self.conversations)
        header = f"""# Conversations Index

Total conversations: **{len(self.conversations)}**
Total messages: **{total_messages}**

"""
        if not self.conversations:
            content = header + "No conversations found.\n"
        else:
            sorted_convs = sorted(self.conversations, key=lambda c: c['mtime'] or datetime.min, reverse=True)
            lines = ["## All Conversations\n\n"]
            for c in sorted_convs:
                title = self.display_title(c)
                updated = self.format_date(c['mtime'])
                summary = self.get_conversation_summary(c)
                fname = c.get('_md_filename', self.safe_filename(title) + '.md')
                lines.append(
                    f"- [{title}](../conversations/{fname})\n"
                    f"  *{summary}* - Model: `{c['model']}` - Modified {updated}\n\n"
                )
            content = header + "".join(lines)

        with open(metadata_dir / "conversations_index.md", 'w', encoding='utf-8') as f:
            f.write(content)

        print("Created metadata files")

    def export_to_markdown(self, output_dir: str) -> None:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        print(f"\nExporting AI Studio archive to: {output_path}")
        print("=" * 60)

        self.create_conversations_structure(output_path)
        self.create_readme(output_path)
        self.create_metadata_files(output_path)

        print("=" * 60)
        print(f"Export complete! Archive created in: {output_path}")
        print(f"Start by opening: {output_path / 'README.md'}")

    def print_summary(self) -> None:
        total_messages = sum(self.message_count(c) for c in self.conversations)
        print("\n" + "=" * 60)
        print("AI STUDIO EXPORT SUMMARY")
        print("=" * 60)
        print(f"Conversations: {len(self.conversations)}")
        print(f"  - Total messages: {total_messages}")
        models = sorted({c['model'] for c in self.conversations})
        print(f"  - Models used: {', '.join(models) if models else 'Unknown'}")
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description='Convert a directory of Google AI Studio chat export JSON files into an indexed markdown archive',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Point at a directory containing many AI Studio .json exports
  python aistudio_complete_parser.py ./aistudio_exports

  # Non-recursive (top-level files only)
  python aistudio_complete_parser.py ./aistudio_exports --no-recursive

  # Or list individual files
  python aistudio_complete_parser.py chat1.json chat2.json chat3.json

  # Custom output directory
  python aistudio_complete_parser.py ./aistudio_exports -o my_archive

  # Drop model "thinking" chunks from the markdown output
  python aistudio_complete_parser.py ./aistudio_exports --no-thoughts

Note: Files are auto-detected by content (must contain a top-level
'chunkedPrompt' key) rather than by filename, since AI Studio doesn't
follow a fixed naming convention the way Claude's export does.
        """
    )

    parser.add_argument('paths', nargs='+', help='Directory of AI Studio export JSON files, and/or individual JSON files')
    parser.add_argument('-o', '--output', default='aistudio_complete_export',
                         help='Output directory for markdown files (default: aistudio_complete_export)')
    parser.add_argument('--no-recursive', action='store_true',
                         help="Don't search subdirectories when a directory is given")
    parser.add_argument('--no-thoughts', action='store_true',
                         help="Exclude model 'thinking' chunks (isThought: true) from the markdown output")

    args = parser.parse_args()

    print("AI Studio Complete Export Parser")
    print("=" * 50)

    parser_instance = AIStudioParser(
        args.paths,
        recursive=not args.no_recursive,
        include_thoughts=not args.no_thoughts,
    )

    parser_instance.load_data()
    parser_instance.print_summary()
    parser_instance.export_to_markdown(args.output)


if __name__ == "__main__":
    main()
