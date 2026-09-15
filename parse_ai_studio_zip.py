import json
import os
import zipfile
from datetime import datetime


def clean_filename(name):
  """Removes characters unsafe for file systems."""
  return "".join(
      c if c.isalnum() or c in (" ", "_", "-") else "_" for c in name
  ).strip()


def parse_ai_studio_archive(zip_path, output_dir):
  if not os.path.exists(zip_path):
    print(
        f"Error: Could not find the zip file at '{zip_path}'. Please check the"
        " filename/path."
    )
    return

  os.makedirs(output_dir, exist_ok=True)
  chats_dir = os.path.join(output_dir, "conversations")
  os.makedirs(chats_dir, exist_ok=True)

  index_rows = []
  processed_count = 0

  print(f"Reading archive: {zip_path}...")
  with zipfile.ZipFile(zip_path, "r") as z:
    for file_info in z.infolist():
      filename = file_info.filename

      # Skip directories/folders
      if file_info.is_dir():
        continue

      try:
        with z.open(file_info) as f:
          content_bytes = f.read()
          data = json.loads(content_bytes.decode("utf-8"))
      except Exception:
        # Skip non-json/binary files quietly
        continue

      # Handle list roots
      if isinstance(data, list):
        items = data
        data = {}
        if items and isinstance(items[0], dict):
          data = items[0]
        data["contents"] = items

      # Validate if it looks like an AI Studio file
      if not isinstance(data, dict) or (
          "runSettings" not in data and "chunkedPrompt" not in data
      ):
        if not any(
            k in data for k in ("title", "name", "messages", "contents")
        ):
          continue

      # 1. Try extracting date from JSON metadata keys
      raw_time = (
          data.get("createTime")
          or data.get("updateTime")
          or data.get("timestamp")
      )
      date_str = None
      sort_datetime = None

      if raw_time:
        try:
          # Parse string to datetime object for proper sorting capability
          clean_time_str = str(raw_time).replace("Z", "+00:00")
          sort_datetime = datetime.fromisoformat(clean_time_str)
          date_str = sort_datetime.strftime("%Y-%m-%d %H:%M")
        except ValueError:
          date_str = str(raw_time)

      # 2. Fallback to ZIP file's internal modification timestamp if JSON lacked it
      if not sort_datetime:
        dt_tuple = file_info.date_time
        if dt_tuple and dt_tuple[0] > 1980:  # Valid FAT timestamp check
          try:
            sort_datetime = datetime(
                dt_tuple[0],
                dt_tuple[1],
                dt_tuple[2],
                dt_tuple[3],
                dt_tuple[4],
                dt_tuple[5],
            )
            date_str = sort_datetime.strftime("%Y-%m-%d %H:%M")
          except Exception:
            pass

      # Ultimate fallback if no timestamps are available
      if not date_str:
        date_str = "Unknown Date"
        # Assign an extremely old date for sorting purposes so they sink to the bottom
        sort_datetime = datetime(1970, 1, 1)

      # Extract metadata
      fallback_name = os.path.basename(filename)
      title = data.get("title") or data.get("name") or fallback_name

      # Extract messages/turns
      chunked_prompt = data.get("chunkedPrompt", {})
      chunks = chunked_prompt.get("chunks", [])

      messages = (
          chunks
          or data.get("contents")
          or data.get("messages")
          or data.get("history")
          or []
      )

      md_content = f"# {title}\n\n* **Created/Modified:** {date_str}\n* **Source File:** `{filename}`\n\n---\n\n"
      first_few_words = "No content snippet available."
      snippet_captured = False

      if isinstance(messages, list):
        for msg in messages:
          if not isinstance(msg, dict):
            continue
          role = msg.get("role", "unknown").upper()

          text_content = msg.get("text", "")
          parts = msg.get("parts") or msg.get("content")

          if not text_content and isinstance(parts, list):
            text_pieces = []
            for p in parts:
              if isinstance(p, dict) and "text" in p:
                text_pieces.append(p["text"])
              elif isinstance(p, str):
                text_pieces.append(p)
            text_content = "\n".join(text_pieces)
          elif not text_content and isinstance(parts, str):
            text_content = parts

          if text_content:
            if not snippet_captured:
              clean_text = " ".join(text_content.split())
              first_few_words = (
                  clean_text[:120] + "..."
                  if len(clean_text) > 120
                  else clean_text
              )
              snippet_captured = True

            md_content += f"### {role}\n\n{text_content}\n\n"

      # Save individual markdown file
      safe_title = clean_filename(title)
      if not safe_title:
        safe_title = "unnamed_chat"
      md_filename = f"{safe_title[:40]}_{processed_count}.md"
      md_filepath = os.path.join(chats_dir, md_filename)

      with open(md_filepath, "w", encoding="utf-8") as out_f:
        out_f.write(md_content)

      index_rows.append({
          "title": title,
          "date": date_str,
          "sort_key": sort_datetime,
          "snippet": first_few_words,
          "link": f"./conversations/{md_filename}",
      })
      processed_count += 1

  # Sort rows datewise: Most recent on top (reverse=True)
  index_rows.sort(key=lambda x: x["sort_key"], reverse=True)

  # Generate Master Index (README.md)
  readme_path = os.path.join(output_dir, "README.md")
  print(f"Generating sorted master index at: {readme_path}...")

  with open(readme_path, "w", encoding="utf-8") as readme:
    readme.write("# Google AI Studio Archive Index\n\n")
    readme.write(
        f"Total Conversations Processed: **{processed_count}** (Sorted most"
        " recent first)\n\n"
    )
    readme.write(
        "| Title | Date | Preview Snippet | Link |\n| :--- | :--- | :--- | :---"
        " |\n"
    )

    for row in index_rows:
      safe_title_tbl = row["title"].replace("|", "-")
      safe_snippet = row["snippet"].replace("|", "-")
      readme.write(
          f"| **{safe_title_tbl}** | {row['date']} | {safe_snippet} |"
          f" [View]({row['link']}) |\n"
      )

  print(
      f"\nSuccess! Processed {processed_count} files into '{output_dir}/'."
      " Open 'README.md' to browse."
  )


if __name__ == "__main__":
  # Configuration variables
  ZIP_PATH = "Google AI Studio.zip"
  OUTPUT_DIR = "ai_studio_markdown_vault"
  parse_ai_studio_archive(ZIP_PATH, OUTPUT_DIR)
