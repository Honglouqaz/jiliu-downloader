#!/usr/bin/env python3
"""i18n 与主题回归（P2-14）。

    python tests/test_i18n.py

两条底线：
  1）缺键不能变成空字符串（否则界面上只剩空气，查都没法查）
  2）主题变量必须成组齐全 —— 少一个 --text，前端就会有一块字看不见
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import config as _config, i18n, theme  # noqa: E402

CFG = dict(_config.DEFAULTS)
ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


print("== 语言归一 ==")
check("空值落默认", i18n.normalize("") == "zh-CN")
check("en 归一到 en-US", i18n.normalize("en") == "en-US", i18n.normalize("en"))
check("en_us 也认", i18n.normalize("en_us") == "en-US", i18n.normalize("en_us"))
check("大小写不敏感", i18n.normalize("EN-us") == "en-US")
check("中文写法国标", i18n.normalize("zh_CN") == "zh-CN")
check("乱写的落回中文", i18n.normalize("klingon") == "zh-CN")

print("== 取词 ==")
check("中文取词", i18n.t("state.done", "zh-CN") == "已完成", i18n.t("state.done", "zh-CN"))
check("英文取词", i18n.t("state.done", "en-US") == "Completed", i18n.t("state.done", "en-US"))
check("不带语言用默认", i18n.t("state.done") == "已完成")
check("缺键原样返回", i18n.t("nope.not.here", "en-US") == "nope.not.here")
check("占位符替换", i18n.t("update.available", "en-US", v="1.8.0") == "Version 1.8.0 available",
      i18n.t("update.available", "en-US", v="1.8.0"))
check("中文占位符", i18n.t("batch.found", "zh-CN", n=7) == "识别到 7 条链接",
      i18n.t("batch.found", "zh-CN", n=7))

print("== 整包 ==")
b_zh = i18n.bundle("zh-CN")
b_en = i18n.bundle("en-US")
check("中英键数一致", len(b_zh) == len(b_en), f"{len(b_zh)} / {len(b_en)}")
check("键集合一致", set(b_zh) == set(b_en))
check("整包无空值", all(v.strip() for v in b_zh.values()))
check("整包无空值（英）", all(v.strip() for v in b_en.values()))
check("前缀过滤", all(k.startswith("queue.") for k in i18n.bundle("zh-CN", "queue.")),
      str(len(i18n.bundle("zh-CN", "queue."))))
check("每条都有两种语言的翻译",
      all(set(row) == {"zh-CN", "en-US"} for row in i18n.STRINGS.values()),
      str([k for k, row in i18n.STRINGS.items() if set(row) != {"zh-CN", "en-US"}][:3]))

print("== 语言列表 / describe ==")
check("两种语言", [x["code"] for x in i18n.languages()] == ["zh-CN", "en-US"])
d = i18n.describe({"ui_lang": "en-US"})
check("describe 反映当前语言", d["current"] == "en-US", d["current"])
check("describe 有键数", d["keys"] == len(i18n.STRINGS), str(d["keys"]))
check("配置里的非法语言被归一", i18n.describe({"ui_lang": "xx"})["current"] == "zh-CN")

print("== 主题：预设与模式 ==")
check("默认预设是 ink", theme.preset_of({}) == "ink", theme.preset_of({}))
check("非法预设落回 ink", theme.preset_of({"ui_theme": "neon"}) == "ink")
check("三种预设", theme.PRESETS == ("ink", "paper", "night"), str(theme.PRESETS))
check("默认模式 auto", theme.mode_of({}) == "auto")
check("非法模式落回 auto", theme.mode_of({"ui_mode": "sepia"}) == "auto")

light = theme.tokens({}, "light")
dark = theme.tokens({}, "dark")
check("浅色底是亮的", light["bg"].startswith("#f"), light["bg"])
check("深色底是暗的", dark["bg"].lower() < "#808080", dark["bg"])
check("浅色文字是暗的", light["text"] < "#808080", light["text"])
check("深色文字是亮的", dark["text"] > "#808080", dark["text"])

print("== 主题：变量完整性 ==")
# 变量名必须和 web/index.html 的 :root 一致，否则前端换主题等于没换
need = {"bg", "chrome", "panel", "panel2", "line", "line2", "text", "dim",
        "dim2", "label", "accent", "accent2", "ok", "err", "warn", "blue", "sel"}
for p in theme.PRESETS:
    for m in ("light", "dark"):
        tk = theme.TOKENS[p][m]
        check(f"{p}/{m} 变量齐全", set(tk) == need,
              str(need - set(tk)) or str(set(tk) - need))
        bad = [k for k in need if not tk[k].startswith("#")]
        check(f"{p}/{m} 颜色都是 #hex", not bad, str(bad))

print("== 主题：切换与 CSS ==")
a = theme.apply({"ui_theme": "paper", "ui_mode": "dark"})
check("apply 回带预设", a["preset"] == "paper" and a["mode"] == "dark")
check("apply 的 tokens 是深色", a["tokens"]["bg"] == theme.TOKENS["paper"]["dark"]["bg"])
check("apply 同时给浅深两套", a["light"] != a["dark"])
css = a["css"]
check("CSS 有 :root", css.startswith(":root{"))
check("CSS 带 --bg", "--bg:" in css and "--text:" in css)
check("CSS 变量数正确", css.count("--") == len(need), str(css.count("--")))

auto = theme.apply({"ui_mode": "auto"})
check("auto 标记为 auto", auto["resolved"] == "auto", auto["resolved"])
check("auto 也给了一份可用 tokens", auto["tokens"]["bg"].startswith("#"))

print("== 主题：强调色 ==")
check("预设色可覆盖", theme.tokens({"ui_accent": "plum"}, "light")["accent"] == theme.ACCENTS["plum"],
      theme.tokens({"ui_accent": "plum"}, "light")["accent"])
check("自定义 hex 可用", theme.tokens({"ui_accent": "#ff8800"}, "light")["accent"] == "#ff8800")
check("非法值不生效（保留预设）",
      theme.tokens({"ui_accent": "notacolor"}, "light")["accent"] == theme.TOKENS["ink"]["light"]["accent"])
check("空值跟随预设",
      theme.tokens({"ui_accent": ""}, "light")["accent"] == theme.TOKENS["ink"]["light"]["accent"])

print("== describe ==")
d = theme.describe({})
check("describe 带预设列表", d["presets"] == ["ink", "paper", "night"])
check("describe 带模式列表", d["modes"] == ["auto", "light", "dark"])
check("describe 带强调色表", any(x["key"] == "jade" for x in d["accents"]))

print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
