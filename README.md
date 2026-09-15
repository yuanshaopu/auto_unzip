# auto_unzip

自动层层解压并集中收集结果的工具（Windows）。

对一个目录做递归扫描，自动收齐跨文件夹分卷压缩包、逐个尝试密码、层层剥壳解压，
最后把结果集中到一个「合集」目录，并把原始压缩包归档到「已处理」目录。

## 功能

- **自动收集分卷**：即使 `.001` / `.002` 分散在不同子文件夹，只要全树内卷号唯一就会自动配对拼接解压；卷号冲突则整组跳过并告警。
- **密码逐个尝试**：先试无密码，再按 `passwords.txt` 顺序尝试；命令行可用 `--password` 追加。
- **层层剥皮**：解压产物里的压缩包会被继续就地解压，直到没有可解压的包。
- **伪装扩展名识别**：头部即 zip/rar/7z 的文件，或「前置数据 + 追加 ZIP」的 polyglot（如 mp4 后拼接 zip），都会按内容识别处理。
- **引擎自动检测**：优先 7-Zip（`7z.exe`），否则用 WinRAR（`Rar.exe` / `WinRAR.exe`）。

## 环境要求

- Windows
- Python 3.7+（或使用打包后的 `auto_unzip.exe`）
- 已安装 [7-Zip](https://www.7-zip.org/) 或 WinRAR（7-Zip 支持格式更全，推荐）

## 使用

```powershell
# 处理指定目录
python auto_unzip.py "D:\some\dir"

# 预览模式(只扫描列出会处理的来源, 不改动任何文件)
python auto_unzip.py "D:\some\dir" --dry-run

# 自检解析规则与解压引擎
python auto_unzip.py --selftest
```

双击 `启动自动解压.bat` 时，会以「程序所在目录」为目标执行；`预览.bat` 为预览模式。

## 密码配置

复制 `passwords.txt`（仓库中不含此文件，需自行创建），每行一个密码，`#` 开头为注释：

```text
mypassword1
mypassword2
```

程序会先试无密码，再按顺序逐个尝试。

## 主要参数

| 参数 | 说明 |
| --- | --- |
| `--collection` | 合集目录名（默认 `解压合集`） |
| `--processed` | 已处理目录名（默认 `已处理`） |
| `--password` | 追加密码，可多次 |
| `--password-file` | 密码文件路径 |
| `--fake-ext` | 疑似伪装扩展名（默认 `.mp4,.tmp`） |
| `--keep-processed` | 保留「已处理」目录里的原压缩包 |
| `--max-extractions` | 单个来源最大解压次数（默认 300） |
| `--timeout` | 单次解压超时秒数（默认 900） |
| `--dry-run` | 仅预览，不改动文件 |

## 打包为 exe

```powershell
pip install pyinstaller
pyinstaller -F -n auto_unzip auto_unzip.py
```

## 许可证

未指定。
