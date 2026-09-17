"""Offline dashboard styles. No remote fonts, scripts or assets."""
CSS = """
:root{color-scheme:dark;--bg:#17191c;--panel:#22252a;--line:#363a41;--fg:#ece9e2;--dim:#a4a5aa;--gold:#d5b46d;--red:#ed9384;--green:#a9c99b;--blue:#a8c2dd}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
a{color:var(--gold)}a:focus-visible,summary:focus-visible{outline:2px solid var(--gold);outline-offset:4px}
header{padding:26px 32px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:18px}
.brand{display:flex;align-items:center;gap:14px}.brand-mark{display:grid;place-items:center;width:44px;height:44px;border:1px solid var(--gold);border-radius:14px;color:var(--gold);font-size:23px}
h1{margin:0;font-size:21px;font-weight:600;letter-spacing:-.03em}header p{margin:0;color:var(--dim);font-size:12px}.meta{color:var(--dim);font-size:12px;text-align:right}
.agent-picker{max-width:1600px;margin:auto;border:0;padding:22px 32px;min-width:0;width:100%}
.agent-picker legend{padding:18px 0 0;color:var(--dim);font-size:12px}
.agent-filter{position:absolute;width:1px;height:1px;opacity:0}
.agent-tab{display:inline-block;margin:0 7px 8px 0;padding:10px 20px;border:1px solid var(--line);border-radius:24px;cursor:pointer;font-weight:500}
.agent-filter:checked+label{background:var(--gold);color:var(--bg);border-color:var(--gold)}
.agent-filter:focus-visible+label{outline:2px solid var(--fg);outline-offset:3px}
main{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-top:14px}
section{grid-column:span 2;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;min-width:0}
section.wide{grid-column:1/-1}h2{font-size:17px;font-weight:550;margin:0 0 14px;letter-spacing:-.02em}h3{font-size:14px;margin:0 0 6px}
p{margin:8px 0}.dim{color:var(--dim)}.small{font-size:12px}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--gold)}
.pill{display:inline-block;padding:3px 9px;border:1px solid currentColor;border-radius:20px;font-size:11px;letter-spacing:.03em;white-space:nowrap}
.agent-owner{display:block;text-transform:uppercase;letter-spacing:.15em;color:var(--gold);font-size:10px;font-weight:650;margin-bottom:7px}
.agent-summary{grid-column:span 1;border-top:2px solid var(--gold);padding:18px}.agent-summary h2{font-size:20px;margin:4px 0}.agent-summary p{font-size:12px;color:var(--dim)}.agent-summary .role{color:var(--fg);font-weight:500}
.section-top{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}.section-top h2{margin:0}
.hero{background:linear-gradient(120deg,#282a2d,#22252a)}.hero-title{font-size:23px}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin:20px 0}
.metric{border-left:2px solid var(--line);padding:0 14px}.metric strong{display:block;font-size:26px;font-weight:550;letter-spacing:-.04em;font-variant-numeric:tabular-nums}.metric span{color:var(--dim);font-size:12px}
.next-action{display:grid;grid-template-columns:minmax(180px,1fr) minmax(220px,2fr);gap:18px;padding:18px;border:1px solid var(--line);border-radius:10px;background:#1b1e22}.eyebrow{font-size:10px;letter-spacing:.14em;color:var(--gold);text-transform:uppercase}
.next-action p{margin:5px 0}.next-action strong{font-size:17px}.notice{border-left:3px solid var(--gold);padding:8px 12px;background:#1d2024;margin:14px 0}.notice.bad{border-color:var(--red)}
.table-scroll{overflow:auto;max-width:100%}table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--dim);font-weight:500}td.num{text-align:right;font-variant-numeric:tabular-nums}tbody tr:hover{background:#2b2f35}
.empty{padding:20px 14px;border:1px dashed var(--line);border-radius:8px;color:var(--dim);font-size:13px}
dl{display:grid;grid-template-columns:minmax(100px,max-content) 1fr;gap:8px 16px;margin:0}dt{color:var(--dim);font-size:12px}dd{margin:0;overflow-wrap:anywhere}ul,ol{padding-left:20px}li{margin:6px 0}
pre{margin:10px 0;padding:12px;background:#17191c;border:1px solid var(--line);border-radius:7px;overflow:auto;font-size:12px}code{font-family:ui-monospace,Consolas,monospace}pre code{white-space:pre}
summary{cursor:pointer;color:var(--dim);padding:8px 0;font-size:13px}details[open]>summary{color:var(--gold);margin-bottom:8px}.reasons{color:var(--red)}
.research-warning{font-size:13px;color:var(--gold)}footer{padding:20px 32px;color:var(--dim);font-size:12px;border-top:1px solid var(--line)}
@media(min-width:1600px){header,footer{padding-left:max(32px,calc((100vw - 1536px)/2));padding-right:max(32px,calc((100vw - 1536px)/2))}}
@media(max-width:1050px){main{grid-template-columns:repeat(2,minmax(0,1fr))}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){header{align-items:flex-start;padding:18px;flex-direction:column}.meta{text-align:left}.agent-picker{padding:16px}.agent-tab{padding:8px 13px;font-size:12px}main{grid-template-columns:minmax(0,1fr)}section,section.agent-summary{grid-column:1/-1;padding:16px}.next-action{grid-template-columns:1fr}.metric{padding-left:10px}.metric strong{font-size:22px}dl{grid-template-columns:1fr}dd{margin-bottom:6px}footer{padding:16px}}
@media print{body{background:white;color:black}.agent-tab{display:none}section{break-inside:avoid}pre{white-space:pre-wrap}}
"""
