# maktabkhoone-downloader
یه اسکریپت پایتون برای دانلود کردن ویدیو های درسی از مکتبخونه

```powershell
pip install -r .\requirements.txt
```
ابندا توی اکانت مکتبخونه لاگین بشین و با افزونه ای مانند Get cookies.txt LOCALLY کوکی های سایت رو دانلود کنید (با استایل نت اسکیپ)
قیل از استفاده دکمه شروع یادگیری رو بزنید
## Basic usage
```bash
python mktabk_downloader.py "https://maktabkhooneh.org/course/%D9%86%D8%B1%D9%85-%D8%A7%D9%81%D8%B2%D8%A7%D8%B1-hspice-mk323/" --cookies maktabkhooneh.org_cookies.txt --out name-of-course --quality high
```

## Options
- `--cookies PATH` (required): Netscape cookie file path
- `--out DIR` (default: `downloads`): output directory
- `--quality auto|high|low` (default: `auto`): preferred quality if multiple MP4s exist on the page
- `--debug`: prints extra info and saves a copy of the fetched course HTML to `<out>/debug_course.html`
- `--course-html PATH`: use a saved course overview HTML instead of fetching (useful if live parsing behaves differently or for offline testing)

## Resume behavior
- Partial downloads are written to `.part` files; if the program is interrupted or your connection drops, re-run the same command and the download will continue where it left off
- Completed files are recognized and skipped
- Existing files are also matched by normalized title (ignores leading index and optional `[high]/[low]` suffix) so the script advances to the next item immediately

## HLS (m3u8) videos
If a lesson only exposes an HLS URL, the script prepares an `ffmpeg` command including cookies and tries to run it. If `ffmpeg` isn’t installed, it will print the command so you can run it manually after installing `ffmpeg`.

## Troubleshooting
- 401/403: export a fresh cookie file (your `sessionid` may have expired)
- No lessons found: add `--debug` and inspect `<out>\debug_course.html`; you can also pass a saved overview page with `--course-html`.
- Sidebar captures too many links: share one saved lesson page; we can tighten the container selector further by class/id.
- Slow or rate-limited: re-run; the script resumes and skips already downloaded content. We can add throttling flags if needed.
