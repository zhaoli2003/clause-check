#!/usr/bin/env python3
"""条款自测 clause-check 助手：校验配置、跑测试用例、试算一组答案、生成页面。只用标准库。

用法：
  python3 clausecheck.py check config.json                     # 校验 + 跑 tests
  python3 clausecheck.py try config.json '{"years": 3, ...}'   # 用一组答案试算，打印每个结论
  python3 clausecheck.py build config.json -o page.html        # 校验 + 跑 tests + 生成完整网页
  python3 clausecheck.py build config.json -o page.html --fragment   # 生成 Claude Artifact 用的片段

表达式规则与页面引擎完全一致：未回答的问题取值为 null（待定），
带 null 的运算结果仍是 null；判断表从上往下找第一条 when 为真的分支，
遇到 when 无法判断（null）就停下，显示"还差几个问题"。
"""
import argparse, datetime as dt, html, json, math, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "assets", "engine.html")
EPS = 1e-9

# ---------------- 表达式 ----------------
def tokenize(s):
    t, i = [], 0
    while i < len(s):
        c = s[i]
        if c.isspace():
            i += 1; continue
        if c.isdigit() or c == ".":
            j = i
            while j < len(s) and (s[j].isdigit() or s[j] == "."): j += 1
            t.append(("n", float(s[i:j]))); i = j; continue
        if c in "\"'":
            j = s.find(c, i + 1)
            if j < 0: raise ValueError("字符串没有结束引号")
            t.append(("s", s[i + 1:j])); i = j + 1; continue
        if c.isalpha() or c == "_" or "一" <= c <= "鿿":
            j = i
            while j < len(s) and (s[j].isalnum() or s[j] == "_" or "一" <= s[j] <= "鿿"): j += 1
            t.append(("id", s[i:j])); i = j; continue
        two = s[i:i + 2]
        if two in ("<=", ">=", "==", "!=", "&&", "||"):
            t.append(("op", two)); i += 2; continue
        if c in "+-*/()<>!,":
            t.append(("op", c)); i += 1; continue
        raise ValueError(f"看不懂的字符「{c}」")
    return t


def parse(src):
    t, p = tokenize(str(src)), [0]
    def is_op(v): return p[0] < len(t) and t[p[0]][0] == "op" and t[p[0]][1] == v
    def expect(v):
        if not is_op(v): raise ValueError(f"缺少「{v}」")
        p[0] += 1
    def primary():
        if p[0] >= len(t): raise ValueError("表达式不完整")
        k, v = t[p[0]]; p[0] += 1
        if k in ("n", "s"): return ("lit", v)
        if k == "id":
            if v == "true": return ("lit", True)
            if v == "false": return ("lit", False)
            if v == "null": return ("lit", None)
            if is_op("("):
                p[0] += 1; args = []
                if not is_op(")"):
                    args.append(or_())
                    while is_op(","):
                        p[0] += 1; args.append(or_())
                expect(")"); return ("call", v, args)
            return ("id", v)
        if k == "op" and v == "(":
            e = or_(); expect(")"); return e
        raise ValueError(f"看不懂「{v}」")
    def unary():
        if is_op("-"): p[0] += 1; return ("neg", unary())
        if is_op("!"): p[0] += 1; return ("not", unary())
        return primary()
    def mul():
        l = unary()
        while is_op("*") or is_op("/"):
            o = t[p[0]][1]; p[0] += 1; l = ("bin", o, l, unary())
        return l
    def addx():
        l = mul()
        while is_op("+") or is_op("-"):
            o = t[p[0]][1]; p[0] += 1; l = ("bin", o, l, mul())
        return l
    def cmp():
        l = addx()
        if p[0] < len(t) and t[p[0]][0] == "op" and t[p[0]][1] in ("<", "<=", ">", ">=", "==", "!="):
            o = t[p[0]][1]; p[0] += 1; return ("bin", o, l, addx())
        return l
    def and_():
        l = cmp()
        while is_op("&&"): p[0] += 1; l = ("and", l, cmp())
        return l
    def or_():
        l = and_()
        while is_op("||"): p[0] += 1; l = ("or", l, and_())
        return l
    e = or_()
    if p[0] < len(t): raise ValueError(f"多余的内容「{t[p[0]][1]}」")
    return e


def ids_in(node, out):
    k = node[0]
    if k == "id": out.add(node[1])
    elif k in ("neg", "not"): ids_in(node[1], out)
    elif k in ("and", "or"): ids_in(node[1], out); ids_in(node[2], out)
    elif k == "bin": ids_in(node[2], out); ids_in(node[3], out)
    elif k == "call":
        for a in node[2]: ids_in(a, out)
    return out


def D(s):
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s): return None
    try: return dt.date.fromisoformat(s)
    except ValueError: return None


def js_round(a, d=0):
    m = 10 ** int(d)
    v = a * m + (EPS if a >= 0 else -EPS)
    return math.floor(v + 0.5) / m


FN = {
    "floor": lambda a: math.floor(a + EPS), "ceil": lambda a: math.ceil(a - EPS), "abs": abs,
    "round": js_round, "min": min, "max": max,
    "remain": lambda s: (dt.date(D(s).year, 12, 31) - D(s)).days + 1,
    "elapsed": lambda s: (D(s) - dt.date(D(s).year, 1, 1)).days + 1,
    "days": lambda a, b: (D(b) - D(a)).days + 1,
    "year": lambda s: D(s).year,
}
DATEFN = {"remain": 1, "elapsed": 1, "year": 1, "days": 2}


def is_num(x): return isinstance(x, (int, float)) and not isinstance(x, bool)


def strict_eq(a, b):
    if is_num(a) and is_num(b): return a == b
    return type(a) is type(b) and a == b


def ev(n, get):
    k = n[0]
    if k == "lit": return n[1]
    if k == "id": return get(n[1])
    if k == "neg":
        v = ev(n[1], get); return None if v is None else -v
    if k == "not":
        v = ev(n[1], get); return None if v is None else (not v)
    if k == "and":
        a, b = ev(n[1], get), ev(n[2], get)
        if a is False or b is False: return False
        if a is None or b is None: return None
        return bool(a and b)
    if k == "or":
        a, b = ev(n[1], get), ev(n[2], get)
        if a is True or b is True: return True
        if a is None or b is None: return None
        return bool(a or b)
    if k == "call":
        if n[1] not in FN: raise ValueError(f"没有函数 {n[1]}()")
        args = [ev(a, get) for a in n[2]]
        if any(a is None for a in args): return None
        if n[1] in DATEFN and any(D(a) is None for a in args[:DATEFN[n[1]]]): return None
        return FN[n[1]](*args)
    if k == "bin":
        o, l, r = n[1], ev(n[2], get), ev(n[3], get)
        if l is None or r is None: return None
        if o == "==": return strict_eq(l, r)
        if o == "!=": return not strict_eq(l, r)
        if o == "+": return l + r
        if o == "-": return l - r
        if o == "*": return l * r
        if o == "/": return None if r == 0 else l / r
        if o == "<": return l < r
        if o == "<=": return l <= r
        if o == ">": return l > r
        if o == ">=": return l >= r
    raise ValueError("无法计算")


_cache = {}
def compile_(src):
    if isinstance(src, bool): src = "true" if src else "false"
    src = str(src)
    if src not in _cache: _cache[src] = parse(src)
    return _cache[src]


# ---------------- 求值（与引擎一致） ----------------
def evaluate(cfg, ans):
    qs = cfg.get("questions") or []
    Q = {q["id"]: q for q in qs}
    vals, vmiss, visible, cur = {}, {}, {}, set()
    def get(i):
        if i in vals:
            if vals[i] is None and i in vmiss: cur.update(vmiss[i])
            return vals[i]
        if i in Q:
            if not visible.get(i): return None
            a = ans.get(i)
            if a is None or a == "":
                cur.add(i); return None
            return a
        raise ValueError(f"不认识「{i}」：它既不是问题，也不是在它之前算出的变量")
    for q in qs:
        visible[q["id"]] = (not q.get("show")) or ev(compile_(q["show"]), get) is True
    def run(item):
        cur.clear()
        cases = item.get("cases") or [{k: item.get(k) for k in ("value", "tone", "text", "why", "cite")}]
        for c in cases:
            w = True if c.get("when") is None else ev(compile_(c["when"]), get)
            if w is True:
                v = None if c.get("value") is None else ev(compile_(str(c["value"])), get)
                if c.get("value") is not None and v is None: return ("pend", sorted(cur), None)
                return ("ok", v, c)
            if w is None: return ("pend", sorted(cur), None)
        d = item.get("default")
        if d:
            v = None if d.get("value") is None else ev(compile_(str(d["value"])), get)
            if d.get("value") is not None and v is None: return ("pend", sorted(cur), None)
            return ("ok", v, d)
        return ("none", sorted(cur), None)
    for x in cfg.get("vars") or []:
        st, v, c = run(x)
        vals[x["id"]] = v if st == "ok" else None
        if st != "ok": vmiss[x["id"]] = set(v or [])
    out = {}
    for x in cfg.get("results") or []:
        show = (not x.get("show")) or ev(compile_(x["show"]), get) is True
        st, v, c = run(x) if show else ("hidden", None, None)
        out[x["id"]] = (st, v, c)
        vals[x["id"]] = ((c or {}).get("tone") if x.get("type") == "verdict" else v) if st == "ok" else None
    return visible, out, vals, get


def fmtv(v, f=None):
    if v is True: return "是"
    if v is False: return "否"
    if not is_num(v): return str(v)
    if f == "money": return f"¥{v:,.2f}"
    if f == "int": return f"{round(v):,}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def interp(s, get):
    def rep(m):
        expr, _, f = m.group(1).partition("|")
        v = ev(compile_(expr), get)
        return "—" if v is None else fmtv(v, f or None)
    return re.sub(r"\{([^{}]+)\}", rep, str(s or ""))


# ---------------- 校验 ----------------
TEMPLATE_RE = re.compile(r"\{([^{}]+)\}")

def validate(cfg):
    errs, warns = [], []
    if not cfg.get("title"): errs.append("缺少 title")
    qs = cfg.get("questions") or []
    if not qs: errs.append("questions 不能为空")
    if not cfg.get("results"): errs.append("results 不能为空")
    CL = cfg.get("clauses") or {}
    docs = {d.get("id") for d in cfg.get("docs") or []}
    for cid, c in CL.items():
        if c.get("doc") and c["doc"] not in docs: errs.append(f"条款 {cid} 的 doc「{c['doc']}」不在 docs 里")
        if c.get("kind") not in ("quote", "summary"): warns.append(f"条款 {cid} 没写 kind（quote 原文摘录 / summary 条文要点）")
        for m in c.get("hl") or []:
            if m not in (c.get("text") or ""): errs.append(f"条款 {cid} 要高亮的「{m}」不在条文里")
    if not CL: errs.append("clauses 为空：没有依据的自测器没有意义")
    seen, qids = set(), set()
    for q in qs:
        if q.get("id") in seen: errs.append(f"问题 id 重复：{q.get('id')}")
        seen.add(q.get("id")); qids.add(q.get("id"))
        if q.get("type") not in ("yesno", "choice", "number", "date"): errs.append(f"问题 {q.get('id')} 的 type 必须是 yesno / choice / number / date")
        if q.get("type") == "choice" and not q.get("options"): errs.append(f"问题 {q.get('id')} 是 choice 但没有 options")
    known_vars = set()
    def check_expr(src, allowed, where):
        try:
            node = compile_(str(src))
        except ValueError as e:
            errs.append(f"{where}：表达式「{src}」写错了：{e}"); return
        for i in ids_in(node, set()):
            if i not in allowed: errs.append(f"{where}：「{i}」不是问题，也不是在它之前定义的变量")
    def check_text(s, allowed, where):
        for m in TEMPLATE_RE.finditer(str(s or "")):
            check_expr(m.group(1).split("|")[0], allowed, where + " 的文字")
    def check_cites(lst, where):
        for c in lst or []:
            if c not in CL: errs.append(f"{where} 引用了不存在的条款「{c}」")
    for q in qs:
        if q.get("show"): check_expr(q["show"], qids, f"问题 {q['id']} 的 show")
        check_cites(q.get("cite"), f"问题 {q['id']}")
    for x in cfg.get("vars") or []:
        allowed = qids | known_vars
        for c in (x.get("cases") or [x]):
            if c.get("when") is not None: check_expr(c["when"], allowed, f"变量 {x['id']}")
            if c.get("value") is not None: check_expr(str(c["value"]), allowed, f"变量 {x['id']}")
        known_vars.add(x["id"])
    done_results = set()
    for r in cfg.get("results") or []:
        allowed = qids | known_vars | done_results
        where = f"结论 {r.get('id')}"
        if r.get("type") not in ("verdict", "value"): errs.append(f"{where}：type 必须是 verdict 或 value")
        if r.get("show"): check_expr(r["show"], allowed, where + " 的 show")
        cases = (r.get("cases") or []) + ([r["default"]] if r.get("default") else [])
        if not r.get("cases") and r.get("value") is None and r.get("text") is None: errs.append(f"{where}：没有 cases")
        for c in cases or [r]:
            if c.get("when") is not None: check_expr(c["when"], allowed, where)
            if c.get("value") is not None: check_expr(str(c["value"]), allowed, where)
            check_text(c.get("why"), allowed, where)
            check_text(c.get("text"), allowed, where)
            if r.get("type") == "verdict" and c.get("tone") not in ("ok", "no", "warn", "info"): errs.append(f"{where}：verdict 分支要有 tone（ok / no / warn / info）")
            if not (c.get("cite") or r.get("cite")): warns.append(f"{where} 有一个分支没有 cite：结论没有依据")
            check_cites(c.get("cite"), where)
        check_cites(r.get("cite"), where)
        done_results.add(r.get("id"))
    everything = qids | known_vars | done_results
    for s in (cfg.get("summary") if isinstance(cfg.get("summary"), list) else ([{"text": cfg["summary"]}] if cfg.get("summary") else [])):
        if s.get("show"): check_expr(s["show"], everything, "summary 的 show")
        check_text(s.get("text"), everything, "summary")
    ex = cfg.get("example")
    if ex is not None:
        for k in (ex.get("answers") or {}):
            if k not in qids: errs.append(f"example.answers 里的「{k}」不是任何问题的 id")
    tests = cfg.get("tests") or []
    if len(tests) < 3: warns.append(f"只有 {len(tests)} 个测试用例；至少写 3 个，覆盖每个结论的主要分支和边界值")
    return errs, warns


def run_tests(cfg):
    fails = []
    for i, tc in enumerate(cfg.get("tests") or [], 1):
        name = tc.get("name") or f"#{i}"
        try:
            _, _, vals, _ = evaluate(cfg, tc.get("answers") or {})
        except ValueError as e:
            fails.append(f"测试「{name}」出错：{e}"); continue
        for k, want in (tc.get("expect") or {}).items():
            got = vals.get(k, "（没有这个结论或变量）")
            ok = (is_num(want) and is_num(got) and abs(got - want) < 0.005) or (not is_num(want) and got == want and type(got) is type(want))
            if not ok: fails.append(f"测试「{name}」：{k} 应为 {want!r}，实际是 {got!r}")
    return fails


def check(cfg):
    errs, warns = validate(cfg)
    for w in warns: print("! " + w)
    for e in errs: print("✗ " + e, file=sys.stderr)
    if errs: return False
    fails = run_tests(cfg)
    n = len(cfg.get("tests") or [])
    for f in fails: print("✗ " + f, file=sys.stderr)
    if not fails: print(f"✓ 配置有效，{n} 个测试用例全部通过")
    return not fails


def try_answers(cfg, ans):
    visible, out, vals, get = evaluate(cfg, ans)
    for r in cfg.get("results") or []:
        st, v, c = out[r["id"]]
        if st == "hidden": continue
        if st != "ok":
            print(f"· {r['label']}：待定（还差：{'、'.join(v or []) or '无法判断'}）"); continue
        shown = interp(c.get("text"), get) if r.get("type") == "verdict" else fmtv(v, r.get("format")) + (r.get("unit") or "")
        print(f"· {r['label']}：{shown}")
        if c.get("why"): print(f"    {interp(c['why'], get)}")
        if c.get("cite") or r.get("cite"): print(f"    依据：{'、'.join(c.get('cite') or r.get('cite'))}")


def build(a):
    cfg = json.load(open(a.config, encoding="utf-8"))
    if not check(cfg): sys.exit(1)
    tpl = open(TEMPLATE, encoding="utf-8").read()
    data = json.dumps(cfg, ensure_ascii=False, indent=2).replace("</", "<\\/")
    block = "/* === CLAUSE CONFIG START === */\nwindow.CLAUSE = " + data + ";\n/* === CLAUSE CONFIG END === */"
    tpl = re.sub(r"/\* === CLAUSE CONFIG START === \*/.*?/\* === CLAUSE CONFIG END === \*/", lambda m: block, tpl, flags=re.S)
    title = html.escape(cfg.get("title", "条款自测").replace("\n", " "))
    tpl = tpl.replace("<title>条款自测</title>", f"<title>{title}</title>", 1)
    if a.fragment:
        body = tpl.split("<!-- CC:FRAGMENT-START -->", 1)[1].split("<!-- CC:FRAGMENT-END -->", 1)[0]
        body = body.replace("<!-- CC:FRAGMENT-BODY -->\n", "")
        tpl = f'<title>{title}</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n' + body.lstrip("\n")
    out = a.output or os.path.splitext(a.config)[0] + ".html"
    open(out, "w", encoding="utf-8").write(tpl)
    print(f"✓ 已生成 {out}")


def main():
    p = argparse.ArgumentParser(description="条款自测 clause-check 助手")
    sp = p.add_subparsers(dest="cmd", required=True)
    c1 = sp.add_parser("check"); c1.add_argument("config")
    c2 = sp.add_parser("try"); c2.add_argument("config"); c2.add_argument("answers", help="JSON，比如 '{\"years\": 3}'")
    c3 = sp.add_parser("build"); c3.add_argument("config"); c3.add_argument("-o", "--output"); c3.add_argument("--fragment", action="store_true")
    a = p.parse_args()
    if a.cmd == "check":
        sys.exit(0 if check(json.load(open(a.config, encoding="utf-8"))) else 1)
    if a.cmd == "try":
        try_answers(json.load(open(a.config, encoding="utf-8")), json.loads(a.answers)); return
    build(a)


if __name__ == "__main__":
    main()
