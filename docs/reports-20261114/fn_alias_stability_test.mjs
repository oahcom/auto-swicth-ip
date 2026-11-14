// fn 工具名中性化稳定性实测：
// 1) 同工具集两次调用（同进程）→ 别名逐字节一致
// 2) 工具集 9→18 增长 → 前 9 个别名不变
// 3) 工具顺序打乱 → 每个名字的别名不变
// 4) 模拟克隆体重跑（新 body 对象同内容）→ 不套第二层
import { applyFingerprintTools } from "/home/administrator/.9router/open-sse/utils/opencodeFingerprint.js";

function mkTool(n) {
  return { type: "function", function: { name: n, description: "desc of " + n, parameters: { type: "object", properties: { a: { type: "string" } }, required: ["a"] } } };
}
function mkBody(names) {
  return { model: "deepseek-v4.1-flash-free", messages: [{ role: "user", content: "hi" }], tools: names.map(mkTool) };
}
function namesOf(body) {
  return body.tools.map((t) => (t.function ? t.function.name : t.name));
}

const setA = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota"];
const extra = ["kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho", "sigma"];
const setB = [...setA, ...extra];

const b1 = mkBody(setA);
applyFingerprintTools(b1, false);
const r1 = namesOf(b1);

const b1b = mkBody(setA);
applyFingerprintTools(b1b, false);
const r1b = namesOf(b1b);

const b2 = mkBody(setB);
applyFingerprintTools(b2, false);
const r2 = namesOf(b2);

const b3 = mkBody([...setA].reverse());
applyFingerprintTools(b3, false);
const r3 = namesOf(b3);
const r3map = new Map(setA.slice().reverse().map((n, i) => [n, r3[i]]));

// 克隆体重跑：structuredClone 后再 transform 不应套第二层
const b4 = structuredClone(mkBody(setA));
applyFingerprintTools(b4, false);
const r4 = namesOf(b4);

console.log("== R1 (9 工具):", r1.join(" | "));
console.log("== R2 (18 工具):", r2.join(" | "));
console.log("1) 同集两次逐字节一致:", JSON.stringify(r1) === JSON.stringify(r1b));
console.log("2) 9->18 前 9 个别名不变:", JSON.stringify(r1.slice(0, 9)) === JSON.stringify(r2.slice(0, 9)));
console.log("   (appendMissing 加的 read/shell 尾巴:", r1.slice(9).join(","), "->", r2.slice(18).join(","), ")");
console.log("3) 顺序打乱后每个名字别名不变:", setA.every((n, i) => r3map.get(n) === r1[i]));
console.log("4) 克隆体不套第二层:", JSON.stringify(r4) === JSON.stringify(r1));
console.log("   R4:", r4.join(" | "));
