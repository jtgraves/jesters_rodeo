import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

# Cache-busting token for /static/style.css. The <link> URL is otherwise
# identical across deploys, so a browser that cached the stylesheet can serve
# a stale copy indefinitely; keying it to the file's bytes forces a re-fetch
# only when the CSS actually changed.
_css = Path("app/static/style.css")
templates.env.globals["static_version"] = (
    hashlib.sha1(_css.read_bytes()).hexdigest()[:8] if _css.exists() else "dev"
)
