"""Shared, self-contained styles for Slate's server-rendered public pages."""

PAGE_CSS = """
:root{color-scheme:light dark;--bg:#f5f5f0;--paper:#fff;--ink:#252e2b;--muted:#66716b;
--line:#dce2dc;--accent:#285e49;--soft:#edf4ed;--shadow:0 16px 56px #233c2910}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 -apple-system,BlinkMacSystemFont,
"Segoe UI",sans-serif;-webkit-text-size-adjust:100%}
a{color:var(--accent);text-underline-offset:4px}
a:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:5px}
.brandbar{max-width:1080px;margin:auto;padding:28px 32px;display:flex;align-items:center;
justify-content:space-between;gap:16px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;font-weight:650;font-size:20px;letter-spacing:-.6px}
.brand-icon{display:inline-grid;place-items:center;width:32px;height:32px;background:var(--accent);
color:var(--paper);border-radius:9px;font:22px Georgia,serif}
.eyebrow{color:var(--muted);font-size:11px;font-weight:650;letter-spacing:1.7px;text-transform:uppercase}
main{width:calc(100% - 64px);max-width:800px;margin:48px auto 64px;background:var(--paper);
border:1px solid var(--line);border-radius:20px;padding:48px 56px;box-shadow:var(--shadow)}
h1{font:400 clamp(30px,5vw,44px)/1.15 Georgia,serif;letter-spacing:-1px;margin:12px 0 16px;
overflow-wrap:anywhere;text-wrap:balance}
h2{font-size:17px;line-height:1.4;letter-spacing:-.25px;margin:32px 0 12px}
p{margin:0 0 16px;overflow-wrap:anywhere}
.date,.t,footer,.hint{color:var(--muted)}
.date{font-size:13px;margin:0 0 32px;padding-bottom:28px;border-bottom:1px solid var(--line)}
.summary-card{background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:24px;margin:28px 0}
.summary-card h2{margin:0 0 8px;color:var(--accent);font-size:11px;letter-spacing:1.5px;text-transform:uppercase}
.tldr{margin:0;font-size:17px;line-height:1.7}
ul{padding-left:22px;margin:0}
li{padding-left:4px;margin:10px 0;overflow-wrap:anywhere;white-space:pre-wrap}
li::marker{color:var(--muted)}
li.ai{list-style:none;margin-left:-22px}
.mark{color:var(--accent);display:inline-block;width:22px}
.t{display:inline-block;font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}
.who{font-weight:650}
.seg{padding:16px 0;margin:0;border-bottom:1px solid var(--line);white-space:pre-wrap}
.transcript{margin-top:32px;padding-top:24px;border-top:1px solid var(--line)}
summary{cursor:pointer;font-size:17px;font-weight:650;padding:8px 0}
summary .hint{display:block;font-size:13px;font-weight:400;margin-left:18px}
footer{margin-top:40px;padding-top:24px;border-top:1px solid var(--line);font-size:12px;line-height:1.8}
.empty{padding:24px;border:1px dashed var(--line);border-radius:12px;color:var(--muted)}
.status-page main{max-width:520px;text-align:center;margin-top:80px;padding:48px 40px}
.status-symbol{display:inline-grid;place-items:center;width:56px;height:56px;border-radius:50%;
background:var(--soft);color:var(--accent);font-size:24px;margin-bottom:16px}
.status-page h1{font-size:34px}
.status-page .message{color:var(--muted);margin-bottom:28px}
.btn{display:block;border-radius:10px;padding:13px 24px;background:var(--accent);color:var(--paper);
text-decoration:none;font-size:14px;font-weight:650}
.hint{font-size:12px;margin:16px 0 0}
@media(max-width:600px){.brandbar{padding:20px}.brandbar>.eyebrow{font-size:9px;letter-spacing:1px}
main{width:calc(100% - 32px);margin:24px auto 32px;padding:28px 24px;border-radius:14px}
.summary-card{padding:20px}.status-page main{margin-top:40px;padding:32px 24px}}
@media(prefers-color-scheme:dark){:root{--bg:#171c19;--paper:#202722;--ink:#e6ece6;--muted:#a9b6ac;
--line:#3b493f;--accent:#aed7b7;--soft:#29382e;--shadow:none}}
@media print{:root{color-scheme:light;--bg:#fff;--paper:#fff;--ink:#000;--muted:#444;
--line:#ccc;--accent:#254c37;--soft:#f5f5f5}.brandbar{padding:0 0 16px}
main{width:100%;max-width:none;margin:24px 0;padding:0;border:0;box-shadow:none}
h2,summary{break-after:avoid}.summary-card,li,.seg{break-inside:avoid}}
"""

BRAND_HEADER = """<header class="brandbar"><div class="brand">
<span class="brand-icon" aria-hidden="true">s</span>Slate</div>
<span class="eyebrow">A little more clarity.</span></header>"""
