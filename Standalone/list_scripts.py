import urllib.request
import re

with open(r'C:\Users\HP\.gemini\antigravity\brain\b4d402cb-ce40-4c39-8fee-f884d453a804\.system_generated\steps\2\content.md', 'r', encoding='utf-8') as f:
    text = f.read()

scripts = re.findall(r'src=["\']([^"\']+\.js[^"\']*)["\']', text)
print("Found script sources:")
for s in set(scripts):
    print(" ", s)
