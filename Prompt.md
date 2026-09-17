### AIStudio parser prompt

_This was the prompt to Claude.ai_

This is a python script which creates a indexed list of all the conversations with claude from the exported json data, and all the conversations in markdown.

Can you please generate a similar script for exported json data from aistudio.google.com, which would be multiple jsons in a directory? Or you can link to some publicly available code to do this, too.

_(the .py file from https://github.com/tyashin/Claude-export-data-parser and a json exported using the Edge plugin at saveai.net were attached.)_

_This produced the aistudio_complete_parser.py which I have not tested, since I don't have json saved in that format._

_Then I prompted Claude again,_

The second uploaded file is a sample exported using the free version of the plugin at saveai.net

_Then it generated saveai_complete_parser.py_

--

### OpenAI parser prompt

For generating the OpenAI export parser, the prompts were:

_OpenAI's data export has given me a zip file with the following files, uploading the directory listing. the chat.html file contains all the conversations with chatgpt, but date/time of the conversation is not shown. Do you need a sample json to create a parser similar to the ones for aistudio and claude, to create markdown from the jsons?_

Claude answered with a yes, and suggested this to generate a small sample - 
```
python3 -c "
import json
data = json.load(open('conversations-000.json'))
json.dump(data[:2], open('sample.json','w'), indent=2)
"
```
Then, it offered to parse the files for me, and I replied, uploading the py file from https://github.com/tyashin/Claude-export-data-parser ,

_Please give me python code to do the parsing locally, and generate index files etc similar to this python script for Claude's data (uploaded)_

and it generated `chatgpt_complete_parser.py`.

### AIStudio zip parser prompt

This prompt was sent to Gemini 3.5 Flash lite via aistudio.google.com

_Please create python code for parsing such a zip archive from the Google AI Studio directory in Google Drive - it should create a directory with the individual conversations in markdown, and an index of all the conversations. A format similar to https://github.com/tyashin/Claude-export-data-parser would be nice - that would include the title of the conversation, the first few words as well as the date in the index of conversations._

The script it created gave an error, which I presented as a follow-up prompt,

_parse_ai_studio_zip.py", line 175, in <module>
parse_ai_studio_archive(ZIP_FILE_PATH, OUTPUT_DIR)
File "/home/sssvv/Downloads/AIStudioConversations/parse_ai_studio_zip.py", line 50, in parse_ai_studio_archive
data.get("title")
^^^^^^^^
AttributeError: 'list' object has no attribute 'get'_

It corrected the script, but then introduced a typo which led to an error which I presented as the next prompt - 

_python3 parse_ai_studio_zip.py File_
_"/home/sssvv/Downloads/AIStudioConversations/parse_ai_studio_zip.py", line 53_
_data = items[0] 0 ^ SyntaxError: invalid syntax_

Then, found that this script was only looking at files with `.json` extension, while the actual conversations did not have the extension. So, next prompt was,

_Unfortunately, the files in the zip file with the actual conversations don't have the json extension. An example file is pasted below, which has the filename "Atmosphere Shader_ _Doubles vs. Floats". The parser script has to be updated to check whether the file has json content, it can't rely on the filename having .json extension.
Pasting the content of an example file below,_ (and I pasted in a json conversation with that filename.)

Now it generated all the conversations as markdown, but with Unknown Date for all of them. So, the next prompt was,

_Instead of writing "Unknown Date" if the date info is not found in the
conversation, please use the date metadata of the file (created date or modified
date) to fill the date field._

And now I had to ask it to sort - 

_And the index should be sorted datewise, with the most recent on top._

This was the script I used as parse_ai_studio_zip.py
