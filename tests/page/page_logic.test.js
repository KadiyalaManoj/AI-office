// Checks the browser office follows the same team, context, Echo and release-gate rules as the Python runner.
// Run: node tests/page/page_logic.test.js
const fs=require("fs");const s=fs.readFileSync(require("path").join(__dirname,"../../office/index.html"),"utf8");
const cut=(a,b)=>{const i=s.indexOf(a),j=s.indexOf(b,i);if(i<0||j<0)throw new Error("missing "+a);return s.slice(i,j)};
let codeFiles=[];
const src=cut("const AGENTS=[","const SAMPLES=")+cut("const LIMITS=","function stepPrompt(")+cut("const GATES=","/* ---------- runner");
(function(){eval(src+";globalThis.T={validTeam,teamText,gateSummary,echoJob,ECHO_JOBS};")})();
const vt=T.validTeam,tt=T.teamText,gs=T.gateSummary,ej=T.echoJob,EJ=T.ECHO_JOBS;
let fails=0;
const ok=(c,m)=>{if(!c){fails++;console.log("FAIL",m)}};
const gated=t=>{const c=t.map((x,i)=>["byte","pixel","terra"].includes(x)?i:-1).filter(i=>i>=0);return c.length&&Math.max(...c)<t.indexOf("lens")&&t.indexOf("lens")<t.indexOf("probe")&&t.indexOf("probe")<t.indexOf("relay")};
ok(gated(vt(["byte","relay","lens"],"feature")),"relay before lens");
const big=vt(["nova","iris","forge","pixel","probe","pulse","quill","echo","nova","byte","pulse","quill","echo"],"feature");
ok(big.length<=12&&gated(big),"cap keeps gate "+big);
const bug=vt(["echo","byte","lens","relay","echo"],"bug");ok(bug[0]==="echo"&&bug.at(-1)==="echo"&&gated(bug),"bug echoes");
ok(!vt(["nova","lens","quill","relay"],"growth").includes("relay"),"no code no relay");
const code="File: a.py\n```python\n"+Array.from({length:900},(_,i)=>`x${i} = ${i}`).join("\n")+"\n```";
ok(tt({outputs:[{agent:"nova",text:"spec"},{agent:"byte",text:code}]},2).includes("x899 = 899"),"full code to reviewer");
const g=gs({outputs:[{agent:"lens",text:"[MAJOR] a: b\nVERDICT: CHANGES REQUESTED"},{agent:"probe",text:"QA: BLOCKED - flaky"}]});
ok(g.blockers.length===2&&g.findings.length===1,"gate blockers "+JSON.stringify(g));
ok(gs({outputs:[{agent:"lens",text:"VERDICT: CHANGES REQUESTED"},{agent:"byte",text:"x"},{agent:"lens",text:"VERDICT: APPROVE"},{agent:"probe",text:"QA: READY"}]}).blockers.length===0,"latest review wins");
ok(ej({kind:"growth",team:["atlas","nova","quill","echo"],idx:3})===EJ.launch,"growth echo");
ok(ej({kind:"bug",team:["atlas","echo","byte"],idx:1})===EJ.triage,"bug triage");
ok(ej({kind:"bug",team:["atlas","echo","byte","lens","probe","relay","echo"],idx:6})===EJ.fixed,"bug close");
ok(ej({kind:"feature",team:["atlas","byte","lens","probe","relay","echo"],idx:5})===EJ.shipped,"feature echo");
console.log(fails?fails+" failed":"all page checks passed");process.exit(fails?1:0);
