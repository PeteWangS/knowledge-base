#!/usr/bin/env python3
"""知识库 Mermaid 图渲染工具 — 主题化 SVG 渲染"""

import sys, base64, urllib.request, os, re

# ═══════════════════════════════════════════════
# 主题色板 — 5 套不同色系
# ═══════════════════════════════════════════════

THEMES = {

    # ──────── K8s / 云原生 — 深海蓝 ────────
    'k8s': {
        'name': 'Ocean Blue 深海蓝',
        'bg': '#0d1117', 'border': '#30363d',
        'primary': '#388bfd', 'text': '#e6edf3',
        'line': '#6e7681', 'edge_bg': '#21262d',
        'cluster_bg': '#0d1117', 'cluster_border': '#30363d',
    },

    # ──────── Docker / 容器 — 碧海青 ────────
    'docker': {
        'name': 'Teal Cyan 碧海青',
        'bg': '#0f172a', 'border': '#1e3a5f',
        'primary': '#22d3ee', 'text': '#e0f2fe',
        'line': '#38bdf8', 'edge_bg': '#0c4a6e',
        'cluster_bg': '#0f172a', 'cluster_border': '#1e3a5f',
    },

    # ──────── Hadoop / 大数据 — 琥珀暖 ────────
    'hadoop': {
        'name': 'Amber Warm 琥珀暖',
        'bg': '#1a1410', 'border': '#3d2e1a',
        'primary': '#fbbf24', 'text': '#fef3c7',
        'line': '#d97706', 'edge_bg': '#292015',
        'cluster_bg': '#1a1410', 'cluster_border': '#3d2e1a',
    },

    # ──────── 网络 — 翠林绿 ────────
    'network': {
        'name': 'Forest Emerald 翠林绿',
        'bg': '#0f1a15', 'border': '#1a3d2e',
        'primary': '#34d399', 'text': '#d1fae5',
        'line': '#6ee7b7', 'edge_bg': '#064e3b',
        'cluster_bg': '#0f1a15', 'cluster_border': '#1a3d2e',
    },

    # ──────── Rocky Linux / 运维 — 苍岭灰 ────────
    'linux-rocky': {
        'name': 'Slate Sage 苍岭灰',
        'bg': '#111815', 'border': '#1e2e25',
        'primary': '#6ee7b7', 'text': '#d1fae5',
        'line': '#5eead4', 'edge_bg': '#134e4a',
        'cluster_bg': '#111815', 'cluster_border': '#1e2e25',
    },
}


def _init_str(theme: str, has_custom_styles: bool) -> str:
    """根据主题和是否有自定义样式生成 init 字符串"""
    t = THEMES.get(theme, THEMES['k8s'])

    cb = t['cluster_bg']
    cs = t['cluster_border']
    bg = t['bg']
    bo = t['border']
    pr = t['primary']
    tx = t['text']
    li = t['line']
    eb = t['edge_bg']

    if has_custom_styles:
        return f"%%{{init: {{'theme': 'base', 'themeVariables': {{'clusterBkg': '{cb}', 'clusterBorder': '{cs}'}}}}}}%%"
    else:
        return (
            f"%%{{init: {{'theme': 'dark', 'themeVariables': {{"
            f"'primaryColor': '{pr}', 'primaryTextColor': '{tx}', "
            f"'primaryBorderColor': '{bo}', 'lineColor': '{li}', "
            f"'secondaryColor': '{bg}', 'tertiaryColor': '{eb}', "
            f"'clusterBkg': '{cb}', 'clusterBorder': '{cs}', "
            f"'nodeBorder': '{bo}', 'nodeTextColor': '{tx}', "
            f"'edgeLabelBackground': '{eb}', 'edgeLabelColor': '{tx}', "
            f"'titleColor': '{tx}'}}}}}}%%"
        )


def _detect_theme(output_path: str) -> str:
    """从输出路径推断主题（assets/{topic}/ → topic）"""
    path = output_path.replace('\\', '/')
    for key in THEMES:
        if f"/{key}/" in path:
            return key
    return 'k8s'


def mermaid_to_svg(mermaid_code: str, output_path: str, theme: str = None) -> bool:
    if theme is None:
        theme = _detect_theme(output_path)

    if "%%{" not in mermaid_code:
        has_custom = bool(re.search(r'style\s+\S+\s+fill:', mermaid_code))
        mermaid_code = _init_str(theme, has_custom) + "\n" + mermaid_code

    b64 = base64.urlsafe_b64encode(mermaid_code.encode('utf-8')).decode('ascii').rstrip('=')
    url = f"https://mermaid.ink/svg/{b64}?type=base64"

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'KB-Bot/2.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            if len(data) < 100:
                return False
            os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
            with open(output_path, 'wb') as f:
                f.write(data)
            print(f"[{theme}] {output_path} ({len(data)} bytes)")
            return True
    except urllib.error.HTTPError as e:
        print(f"[{theme}] HTTP {e.code}: {output_path}")
        return False
    except Exception as e:
        print(f"[{theme}] 失败: {e}: {output_path}")
        return False


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("用法: render-mermaid.py <mmd文件|-> <输出SVG> [--theme <主题名>]")
        print(f"主题: {', '.join(THEMES.keys())}")
        sys.exit(1)

    src = sys.stdin.read() if sys.argv[1] == '-' else open(sys.argv[1]).read()
    out = sys.argv[2]
    theme = None
    if '--theme' in sys.argv:
        idx = sys.argv.index('--theme') + 1
        theme = sys.argv[idx] if idx < len(sys.argv) else None

    ok = mermaid_to_svg(src, out, theme)
    sys.exit(0 if ok else 1)
