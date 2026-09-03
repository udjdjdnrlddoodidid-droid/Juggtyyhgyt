# اتصال FilmBuff به Cloudflare Worker + R2

## نام‌ها را دقیقاً همین بگذار

### Secret مشترک Railway و Cloudflare Worker
`FILMBUFF_SUBTITLE_API_KEY`

مقدار این Secret در هر دو طرف باید **دقیقاً یکسان** باشد.

### آدرس سرویس Railway در Cloudflare Worker
`FILMBUFF_SUBTITLE_API_URL`

مثال:
`https://filmbuff-subtitle-production.up.railway.app`

### R2 Binding در Worker
`SUBTITLE_CACHE`

### نام پیشنهادی Bucket
`filmbuff-subtitles-cache`

## Cloudflare Dashboard

1. R2 Object Storage -> Create bucket
2. نام bucket: `filmbuff-subtitles-cache`
3. Worker فعلی FilmBuff -> Settings -> Bindings
4. Add binding -> R2 bucket
5. Variable name / Binding: `SUBTITLE_CACHE`
6. Bucket: `filmbuff-subtitles-cache`
7. Save/Deploy

سپس Worker -> Settings -> Variables and Secrets:

- Secret: `FILMBUFF_SUBTITLE_API_KEY` = همان مقدار Railway
- Variable یا Secret: `FILMBUFF_SUBTITLE_API_URL` = دامنه Railway بدون / آخر

اختیاری، فقط اگر CDN جدید اضافه شد:
- Variable: `SUBTITLE_CDN_HOSTS=abrtech.top,ir.cdn.ir,example-cdn.com`

همان دامنه جدید را در Railway Variable به نام `ALLOWED_HOST_SUFFIXES` هم اضافه کن.

## Wrangler TOML

```toml
[[r2_buckets]]
binding = "SUBTITLE_CACHE"
bucket_name = "filmbuff-subtitles-cache"
```

Secretها:

```bash
npx wrangler secret put FILMBUFF_SUBTITLE_API_KEY
npx wrangler secret put FILMBUFF_SUBTITLE_API_URL
```

ساخت R2 با Wrangler:

```bash
npx wrangler r2 bucket create filmbuff-subtitles-cache
```
