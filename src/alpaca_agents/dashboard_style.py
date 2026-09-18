"""Offline dashboard styles. No remote fonts or assets."""
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
/* Interactive crew workspace */
body{background:radial-gradient(ellipse at 12% 0%,#302d2638,transparent 45%),var(--bg)}
.workspace{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:24px;padding:0 28px;max-width:1800px;margin:0 auto;align-items:start}
.agent-picker{padding:22px 0;margin:0;max-width:none}.agent-picker legend{padding-top:22px}
.header-actions{display:flex;gap:22px;align-items:center}.chat-jump{text-decoration:none;font-size:13px;border-bottom:1px solid var(--gold);padding-bottom:3px}
.agent-tab{font-size:13px;padding:9px 18px;transition:background .16s,border-color .16s}.agent-tab:hover{border-color:var(--gold)}
.agent-summary{display:flex;flex-direction:column;position:relative;overflow:hidden;padding:16px;border-top:1px solid var(--line);transition:transform .18s,border-color .18s;background:linear-gradient(160deg,#292c31,#202328)}
.agent-summary:hover{transform:translateY(-3px);border-color:var(--accent)}
.agent-houston{--accent:#eabd76;--avatar-bg:#e7ca97}.agent-star{--accent:#f09480;--avatar-bg:#ecb2a6}.agent-moon{--accent:#a2c5ef;--avatar-bg:#b9cfe8}.agent-astra{--accent:#a5c8b5;--avatar-bg:#b8d1c1}
.agent-summary .eyebrow{color:var(--accent);font-size:9px;margin-top:13px;min-height:28px}.agent-summary h2{margin:0;font-size:22px}.agent-summary .role{font-size:11px;margin:5px 0}.agent-summary p{font-size:11px;line-height:1.5}
.avatar-stage{background:var(--avatar-bg);border-radius:10px;display:flex;justify-content:center;align-items:center;height:130px;flex-shrink:0;position:relative}
.avatar-stage:before{content:'';position:absolute;border:1px solid #ffffff55;border-radius:50%;width:92px;height:92px}.avatar-stage img{position:relative;object-fit:contain;max-width:100%;filter:drop-shadow(0 6px 3px #00000022);transition:transform .22s}
.agent-summary:hover img{transform:rotate(-4deg) scale(1.035)}
button,select,textarea{font:inherit}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.45}button:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--gold);outline-offset:3px}
.talk-button{display:flex;justify-content:space-between;gap:8px;width:100%;border:0;border-top:1px solid var(--line);padding:12px 0 0;margin-top:auto;color:var(--accent);background:none;font-size:12px;text-align:left}
.hero{border-color:#565044;background:linear-gradient(120deg,#292a2b,#202328)}.hero-title{font-size:21px}.metric strong{font-size:22px}.metric{padding:0 10px}.metrics{gap:8px}.next-action{gap:12px;grid-template-columns:minmax(150px,1fr) minmax(200px,2fr)}
.chat-dock{position:sticky;top:20px;margin:26px 0 24px;border:1px solid #49443b;border-radius:18px;background:linear-gradient(160deg,#292b2f,#1c1f23);padding:18px;display:flex;flex-direction:column;gap:10px;height:min(940px,calc(100dvh - 40px));min-height:620px;box-shadow:0 18px 60px #00000025;min-width:0;scroll-margin-top:20px}
.chat-heading h2{font-size:21px;margin:4px 0}.chat-heading p{color:var(--dim);font-size:11px;margin:0}.chat-heading .eyebrow{font-size:9px}
.chat-label{color:var(--dim);font-size:11px;display:block;margin:0 0 4px}#chat-agent{width:100%;background:#17191c;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px}
.chat-persona{display:flex;align-items:center;gap:12px}.chat-persona img{object-fit:contain;background:#c4d1c5;border-radius:12px;padding:3px}.chat-persona strong{display:block;font-size:17px}.chat-persona span{color:var(--dim);font-size:11px;display:block}.chat-persona .local-badge{margin-left:auto;font-size:9px;border:1px solid var(--line);padding:3px 6px;border-radius:5px;letter-spacing:.09em}
.chat-snapshot{font-size:10px;color:var(--dim);margin:0;padding-bottom:8px;border-bottom:1px solid var(--line)}
#chat-log{flex:1;min-height:160px;overflow-y:auto;overscroll-behavior:contain;padding:2px 4px 5px 0;scrollbar-width:thin;scrollbar-color:var(--line) transparent}.chat-placeholder{color:var(--dim);font-size:12px}
.chat-message{border:1px solid var(--line);border-radius:10px;padding:12px;margin:0 0 10px;background:#25292f;font-size:12px;overflow-wrap:anywhere}.chat-message p{white-space:pre-wrap;margin:6px 0}.message-name{font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--gold)}.message-source{display:block;color:var(--dim);font-size:10px;margin-top:9px;padding-top:7px;border-top:1px solid var(--line)}
.from-user{margin-left:26px;background:#39342a;border-color:#5a4d36}.from-user .message-name{color:#ead9b3}
.message-head{display:flex;align-items:center;gap:8px;margin-bottom:2px}.message-avatar{width:22px;height:24px;border-radius:6px;background:#c4d1c5 center/contain no-repeat;flex-shrink:0}.message-time{margin-left:auto;font-size:10px;color:var(--dim);font-variant-numeric:tabular-nums}
.message-body{font-size:12.5px;line-height:1.55}.message-body p{white-space:normal;margin:6px 0}.message-body ul,.message-body ol{margin:6px 0 6px 18px;padding:0}.message-body li{margin:3px 0}.message-body strong{color:#f1e6c8}.message-body code{background:#16191d;border:1px solid var(--line);border-radius:4px;padding:0 4px;font-size:11px}.message-heading{font-weight:600;color:var(--gold);margin:10px 0 2px!important;font-size:12px;letter-spacing:.03em}.message-body a{color:var(--gold);text-decoration:underline dotted;overflow-wrap:anywhere}
.thinking{display:flex;align-items:center;gap:8px;color:var(--dim);font-size:12px;border-style:dashed}.thinking .dots{letter-spacing:3px;animation:pulse 1.2s infinite}@keyframes pulse{0%,100%{opacity:.25}50%{opacity:1}}
.chat-buttons{display:flex;gap:12px}.nova-button{background:linear-gradient(120deg,#c9a961,#e2c98a);color:#181a1d;border:0;border-radius:10px;padding:10px 12px;font-size:12px;font-weight:600;letter-spacing:.02em;cursor:pointer}.nova-button:hover{filter:brightness(1.06)}#chat-usage{font-variant-numeric:tabular-nums}
body.chat-wide .workspace{grid-template-columns:minmax(0,1fr) minmax(480px,44%)}body.chat-wide .chat-dock{height:min(1100px,calc(100dvh - 40px))}body.chat-wide main{grid-template-columns:repeat(2,minmax(0,1fr))}
#chat-prompts{display:flex;gap:6px;flex-wrap:wrap}.prompt-chip{background:transparent;color:var(--gold);border:1px solid #514737;border-radius:8px;padding:6px 8px;font-size:10px;text-align:left}.prompt-chip:hover{background:#383228}
.composer{display:flex;align-items:flex-end;gap:6px;padding:8px;background:#16191d;border:1px solid #55504a;border-radius:10px}.composer textarea{resize:vertical;min-height:42px;max-height:120px;flex:1;min-width:0;background:none;border:0;color:var(--fg);font-size:12px;padding:4px}.composer button{background:var(--gold);color:#181a1d;border:0;border-radius:8px;width:34px;height:34px;font-size:22px;flex-shrink:0}
.chat-bottom{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:9px;color:var(--dim)}.chat-bottom button{background:none;border:0;color:var(--gold);padding:0;font-size:10px}.chat-privacy{font-size:10px;color:var(--dim);margin:0;line-height:1.5}
@media(min-width:1600px){header,footer{padding-left:max(28px,calc((100vw - 1744px)/2));padding-right:max(28px,calc((100vw - 1744px)/2))}}
@media(max-width:1350px) and (min-width:1051px){.workspace{grid-template-columns:minmax(0,1fr) 330px;gap:18px;padding:0 20px}main{grid-template-columns:repeat(2,minmax(0,1fr))}.agent-summary{grid-column:span 1}.avatar-stage{height:115px}.avatar-stage img{height:108px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.next-action{grid-template-columns:1fr}}
@media(max-width:1050px){.workspace{grid-template-columns:minmax(0,1fr);max-width:900px}.chat-dock{position:relative;top:auto;max-height:850px;height:850px;width:100%;margin-top:0}.agent-summary{grid-column:span 1}.header-actions{gap:12px;flex-wrap:wrap}}
@media(max-width:600px){.workspace{padding:0 16px}.agent-picker{padding:16px 0}.agent-picker legend{padding-top:8px}.header-actions{width:100%;justify-content:space-between;gap:12px}.meta{font-size:10px}.chat-jump{font-size:12px}.agent-summary{display:grid;grid-template-columns:100px minmax(0,1fr);column-gap:16px}.avatar-stage{grid-column:1;grid-row:1/6;height:135px;align-self:center}.avatar-stage img{width:94px;height:124px}.agent-summary .eyebrow{margin:0;min-height:0}.agent-summary .talk-button{font-size:11px}.agent-summary:hover{transform:none}.chat-dock{padding:16px;min-height:650px;height:820px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.next-action{grid-template-columns:1fr}.hero-title{font-size:19px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{transition:none!important;animation:none!important;scroll-behavior:auto!important}.agent-summary:hover,.agent-summary:hover img{transform:none}}
@media print{body{background:white;color:black}.agent-tab,.chat-dock,.talk-button,.chat-jump{display:none}.workspace{display:block}section{break-inside:avoid}pre{white-space:pre-wrap}}
"""
