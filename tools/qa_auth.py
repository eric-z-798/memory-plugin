"""Synthetic capability for isolated demo servers; never a production CLI flag."""
import json
import urllib.request
TOKEN = 'synthetic-browser-qa-token-only'
BOOTSTRAP = "import secrets; secrets.token_urlsafe=lambda n: '" + TOKEN + "'; import run; run.main()"

def urlopen(url, *args, **kwargs):
    request = urllib.request.Request(url) if isinstance(url, str) else url
    request.add_header('X-Xingshu-Token', TOKEN)
    return urllib.request.urlopen(request, *args, **kwargs)

def authenticate(page, token=TOKEN):
    page.context.set_extra_http_headers({'X-Xingshu-Token':token})
    page.add_init_script('sessionStorage.setItem("xingshu.accessToken",'+json.dumps(token)+');')

async def authenticate_async(page, token=TOKEN):
    await page.context.set_extra_http_headers({'X-Xingshu-Token':token})
    await page.add_init_script('sessionStorage.setItem("xingshu.accessToken",'+json.dumps(token)+');')
