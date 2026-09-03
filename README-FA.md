# FilmBuff Railway Subtitle Engine

این سرویس فقط ترک زیرنویس داخل MKV را با ffprobe/ffmpeg استخراج و به WebVTT تبدیل می‌کند.
ویدئو از Railway به کاربر پروکسی نمی‌شود.

## Variableهای Railway

حتماً این Variable را بساز:

- `FILMBUFF_SUBTITLE_API_KEY` = یک رشته تصادفی و طولانی. **همین مقدار باید Secret همان نام در Cloudflare Worker باشد.**

پیشنهادی/پیش‌فرض:

- `ALLOWED_HOST_SUFFIXES=abrtech.top,ir.cdn.ir`
- `MAX_CONCURRENT_EXTRACTIONS=1`
- `PROBE_TIMEOUT_SECONDS=45`
- `EXTRACT_TIMEOUT_SECONDS=210`

## Deploy

فایل‌ها را در یک GitHub repo قرار بده و در Railway از Deploy from GitHub Repo استفاده کن.
Railway فایل `Dockerfile` ریشه پروژه را خودکار تشخیص می‌دهد.
بعد از Deploy از Settings/Networking یک Public Domain بساز.

URL به شکل زیر می‌شود:

`https://YOUR-SERVICE.up.railway.app`

این URL را در Cloudflare Worker با نام `FILMBUFF_SUBTITLE_API_URL` قرار بده.

## تست

Health:

`GET /health`

Probe ترک‌ها:

`GET /probe?url=MKV_URL`

Extract:

`GET /extract?url=MKV_URL`

برای `/probe` و `/extract` هدر زیر لازم است:

`Authorization: Bearer YOUR_FILMBUFF_SUBTITLE_API_KEY`

## مصرف کم Railway

- `MAX_CONCURRENT_EXTRACTIONS=1` بماند.
- Railway Serverless/Sleep را از تنظیمات سرویس فعال کن اگر برای پلنت در دسترس است.
- Cache اصلی روی Cloudflare R2 است؛ هر ویدئو بعد از اولین استخراج دیگر نباید Railway را درگیر کند.
