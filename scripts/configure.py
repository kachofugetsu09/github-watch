"""显式保存 GitHub Watch 配置；PEM 内容仍由原文件拥有。"""

from __future__ import annotations

import argparse
from pathlib import Path
import tomllib

from agent.plugin_composition.config_input import save_config, upgrade_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-file", type=Path)
    source.add_argument("--upgrade", action="store_true")
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()
    if args.upgrade:
        backup = upgrade_config(
            data_dir, lambda content: tomllib.loads(content.decode("utf-8")),
        )
        print(f"配置已升级；恢复点：{backup}")
    else:
        save_config(data_dir, tomllib.loads(args.from_file.read_text(encoding="utf-8")))
        print("配置已保存")


if __name__ == "__main__":
    main()
