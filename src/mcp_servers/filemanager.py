import sys as _sys
import os as _os
_src_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)

import os
from mcp.server.fastmcp import FastMCP

from webot.workspace import resolve_session_workspace

mcp = FastMCP("FileManager")

def _user_dir(username: str, session_id: str = "") -> str:
    """获取用户目录路径，自动创建。

    :param username: 用户名
    :return: 用户目录的绝对路径
    """

    return str(resolve_session_workspace(username, session_id).root)

def _safe_path(username: str, filename: str, session_id: str = "") -> str:
    """拼接安全路径，防止路径穿越。

    :param username: 用户名
    :param filename: 文件名
    :return: 文件的完整绝对路径
    :raises ValueError: 如果路径穿越尝试被检测到
    """
    user_path = _user_dir(username, session_id)
    full_path = os.path.normpath(os.path.join(user_path, filename))
    # 确保路径在用户目录内
    if not full_path.startswith(user_path):
        raise ValueError(f"非法路径: {filename}")
    return full_path

@mcp.tool()
async def list_files(username: str, session_id: str = "") -> str:
    """
    列出当前用户的所有文件。

    :param username: 用户名（由系统自动注入，无需手动传递）
    :return: 用户文件列表的描述
    """
    user_path = _user_dir(username, session_id)
    try:
        files = os.listdir(user_path)
        if not files:
            return "📂 你还没有任何文件。"
        result = "📂 你的文件列表：\n"
        for file_name in sorted(files):
            file_path = os.path.join(user_path, file_name)
            size = os.path.getsize(file_path)
            if size < 1024:
                size_str = f"{size} B"
            else:
                size_str = f"{size / 1024:.1f} KB"
            result += f"  - {file_name} ({size_str})\n"
        return result
    except Exception as e:
        return f"⚠️ 列出文件失败: {str(e)}"

@mcp.tool()
async def read_file(username: str, filename: str, session_id: str = "") -> str:
    """
    读取用户的指定文件内容。

    :param username: 用户名（由系统自动注入，无需手动传递）
    :param filename: 要读取的文件名
    :return: 文件内容或错误信息
    """
    try:
        file_path = _safe_path(username, filename, session_id)
        if not os.path.exists(file_path):
            return f"❌ 文件 '{filename}' 不存在。"
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content:
            return f"📄 文件 '{filename}' 是空的。"
        return f"📄 文件 '{filename}' 的内容：\n\n{content}"
    except ValueError as e:
        return f"❌ {str(e)}"
    except Exception as e:
        return f"⚠️ 读取文件失败: {str(e)}"

@mcp.tool()
async def write_file(username: str, filename: str, content: str, session_id: str = "") -> str:
    """
    创建或覆盖写入用户的指定文件。

    :param username: 用户名（由系统自动注入，无需手动传递）
    :param filename: 要写入的文件名
    :param content: 要写入的内容
    :return: 操作结果描述
    """
    try:
        file_path = _safe_path(username, filename, session_id)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 文件 '{filename}' 已保存。"
    except ValueError as e:
        return f"❌ {str(e)}"
    except Exception as e:
        return f"⚠️ 写入文件失败: {str(e)}"

@mcp.tool()
async def append_file(username: str, filename: str, content: str, session_id: str = "") -> str:
    """
    向用户的指定文件末尾追加内容。

    :param username: 用户名（由系统自动注入，无需手动传递）
    :param filename: 要追加内容的文件名
    :param content: 要追加的内容
    :return: 操作结果描述
    """
    try:
        file_path = _safe_path(username, filename, session_id)
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 内容已追加到 '{filename}'。"
    except ValueError as e:
        return f"❌ {str(e)}"
    except Exception as e:
        return f"⚠️ 追加文件失败: {str(e)}"

@mcp.tool()
async def delete_file(username: str, filename: str, session_id: str = "") -> str:
    """
    删除用户的指定文件。

    :param username: 用户名（由系统自动注入，无需手动传递）
    :param filename: 要删除的文件名
    :return: 操作结果描述
    """
    try:
        file_path = _safe_path(username, filename, session_id)
        if not os.path.exists(file_path):
            return f"❌ 文件 '{filename}' 不存在，无法删除。"
        os.remove(file_path)
        return f"🗑️ 文件 '{filename}' 已删除。"
    except ValueError as e:
        return f"❌ {str(e)}"
    except Exception as e:
        return f"⚠️ 删除文件失败: {str(e)}"

if __name__ == "__main__":
    mcp.run()
