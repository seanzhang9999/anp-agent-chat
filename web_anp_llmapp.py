"""DID WBA Example with both Client and Server capabilities."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
import time
from typing import Any, Dict
from click.core import F
from loguru import logger
import uvicorn
import datetime
import yaml
from anp_core.client.client import ANP_req_auth, ANP_req_chat,chat_to_did

from anp_core.auth.did_auth import (
    generate_or_load_did, 
    send_authenticated_request,
    send_request_with_token,
    DIDWbaAuthHeader
)

from config import dynamic_config

unique_id = None

# 尝试导入httpx，如果不存在则在需要时提示安装
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    print("警告: 缺少httpx模块，某些功能将不可用。请使用 'pip install httpx' 安装该模块。")

from api.anp_nlp_router import (
    resp_handle_request_msgs,
    resp_handle_request_new_msg_event as server_new_message_event,
)
from core.app import create_app
from core.config import settings
from anp_core.server.server import ANP_resp_start, ANP_resp_stop, server_status
from anp_core.client.client import ANP_req_auth, ANP_req_chat
from utils.log_base import set_log_color_level
user_dir = os.path.dirname(os.path.abspath(__file__))
user_dir = os.path.join(user_dir, "logs")
# 设置日志
logger.add(f"{user_dir}/anp_llmapp_web.log", rotation="1000 MB", retention="7 days", encoding="utf-8")

# Create FastAPI application
app = create_app()


@app.get("/", tags=["status"])
async def root():
    """
    Root endpoint for server status check.
    
    Returns:
        dict: Server status information
    """
    return {
        "status": "running",
        "service": "DID WBA Example",
        "version": "0.1.0",
        "mode": "Client and Server",
        "documentation": "/docs"
    }


# 全局变量，用于存储服务器、客户端和聊天线程
server_thread = None
chat_thread = None
server_running = False
chat_running = False
server_instance = None  # 存储uvicorn.Server实例

# 全局变量，用于存储最新的聊天消息
client_chat_messages = []
# 事件，用于通知聊天线程有新消息
client_new_message_event = asyncio.Event()




def resp_start(port=None):
    """启动服务器线程
    
    Args:
        port: 可选的服务器端口号，如果提供则会覆盖默认端口
    """
    # 如果提供了端口号，则临时修改设置中的端口
    if port is not None:
        try:
            port_num = int(port)
            dynamic_config.set('web_anp_llmapp.user-did-port',port_num)
            logger.info(f"Use a custom port: {port_num}")
        except ValueError:
            port_num = dynamic_config.get('web_anp_llmapp.user-did-port')
            logger.error(f"Error prot : {port}，use default port: {port_num}")
    else:
        # 如果未提供端口号，则使用配置中的端口
        port = dynamic_config.get('web_anp_llmapp.user-did-port')

    # 调用did_core中的start_server函数
    return ANP_resp_start(port=port)


def resp_stop():
    """停止服务器线程"""
    # 调用did_core中的stop_server函数
    return ANP_resp_stop()


async def run_chat():
    """运行LLM聊天线程 - 直接调用OpenRouter API"""
    global chat_running

    try:
        # 检查是否安装了httpx模块
        if not HTTPX_AVAILABLE:
            print("[错误] 缺少httpx模块，无法启动聊天线程。请使用 'pip install httpx' 安装该模块。")
            chat_running = False
            return
            
        # 导入ANP-NLP路由器中的事件和消息
        from api.anp_nlp_router import resp_handle_request_new_msg_event, resp_handle_request_msgs, notify_chat_thread
        
        # 检查OpenRouter API密钥是否配置
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not openrouter_api_key:
            print("[错误] 未配置OpenRouter API密钥，请在环境变量中设置OPENROUTER_API_KEY")
            chat_running = False
            return
            
        # OpenRouter API配置
        openrouter_api_url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {openrouter_api_key}",
            "Content-Type": "application/json"
        }
        
        print("\n已启动LLM聊天线程。输入消息与AI对话，输入 /q 退出。")
        print("特殊命令: 输入 @[agent-name]:[msg] 。")
        logging.info("聊天线程启动 - 直接调用OpenRouter API")
        
        # 进入聊天模式
        async with httpx.AsyncClient(timeout=30) as client:
            chat_running = True
            while chat_running:
                user_msg = input("你: ").strip()
                
                if user_msg.lower() == "/q":
                    print("退出聊天线程。\n")
                    break
                
# 处理特殊命令 @anp-bot
                if user_msg.strip().startswith("@") and user_msg.strip().find(" ") :
                    parts = user_msg.strip().split(" ", 1)
                    agentname = parts[0].strip().split("@", 1)
                    agentname = agentname[1]
                    bookmark_config_dir = os.path.dirname(os.path.abspath(__file__))
                    bookmark_config_dir = os.path.join(bookmark_config_dir, "anp_core", "anp_bookmark")
                    os.makedirs(bookmark_config_dir, exist_ok=True)
                    bookmark_config_file = os.path.join(bookmark_config_dir, f"{agentname}.js")
                    if os.path.exists(bookmark_config_file):
                    # 读取已有的配置文件
                        print(f"找到智能体书签文件: {bookmark_config_file}")
                        with open(bookmark_config_file, 'r', encoding='utf-8') as f:
                            config_data = json.loads(f.read())
                            agentname = config_data.get('name')
                            did = config_data.get('did')
                            url = config_data.get('url')
                            port = config_data.get('port')
                        print(f"使用{agentname}智能体DID: {did}地址：{url}端口：{port}通讯")
                        os.environ['target-port'] = f"{port}"
                        os.environ['target-host'] = f"{url}"                        
                        custom_msg = "ANPbot的问候，请二十字内回复我"
                        if len(parts) > 1 and parts[1].strip():
                            custom_msg = parts[1].strip()
                        print(f"将向智能体{port}发送消息: {custom_msg}")
                        chat_running = False
                        # 获取token，如果环境变量中不存在则使用None
                        token = os.environ.get('did-token', None)
                        # 调用send_msg函数发送消息（非阻塞方式）
                        chat_to_ANP(custom_msg, token, unique_id)
                        
                        print("\n客户端执行中，你可以先聊。")
                        chat_running = True
                    else:
                        print(f"您要交流的智能体{agentname}不存在，请通过智能体搜索寻找合适的智能体")# 生成或加载智能体的DID
                    continue
                    
                try:
                    # 准备请求数据
                    payload = {
                        "model": "deepseek/deepseek-chat-v3-0324:free",  # 免费模型
                        "messages": [{"role": "user", "content": user_msg}],
                        "max_tokens": 512
                    }
                    
                    # 发送请求到OpenRouter API
                    resp = await client.post(
                        openrouter_api_url,
                        headers=headers,
                        json=payload
                    )
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        answer = data['choices'][0]['message']['content']
                        print(f"助手: {answer}")
                    else:
                        print(f"[错误] OpenRouter API返回: {resp.status_code} {resp.text}")
                except Exception as ce:
                    print(f"白嫖的OpenRouter生气了: {ce}")
                    if not chat_running:  # 如果线程被外部终止
                        break
                
    except Exception as e:
        logging.error(f"聊天线程出错: {e}")
    finally:
        chat_running = False


def start_chat():
    """启动LLM聊天线程 - 直接调用OpenRouter API，同时自动启动服务器"""
    global chat_thread, chat_running, server_thread, server_running
    
    # 检查聊天线程是否已在运行
    if chat_thread and chat_thread.is_alive():
        print("聊天线程已经在运行中")
        return
    
    # 检查服务器是否已在运行，如果没有则自动启动（静默模式）
    if not server_thread or not server_thread.is_alive():
        # 启动服务器线程（不打印日志信息）
        original_log_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.ERROR)  # 只显示错误日志
        
        server_thread = threading.Thread(target=resp_start, daemon=True)
        server_thread.start()
        
        # 等待服务器启动
        time.sleep(1)
    
    # 启动聊天线程
    chat_thread = threading.Thread(target=lambda: asyncio.run(run_chat()), daemon=True)
    chat_thread.start()
    print("LLM聊天线程已启动")


def stop_chat():
    """停止LLM聊天线程，同时处理自动启动的服务器线程"""
    global chat_thread, chat_running, server_thread, server_running, server_instance
    if not chat_thread or not chat_thread.is_alive():
        print("聊天线程未运行")
        return
    
    print("正在关闭聊天线程...")
    chat_running = False
    chat_thread.join(timeout=5)
    if chat_thread.is_alive():
        print("聊天线程关闭超时，可能需要重启程序")
    else:
        print("聊天线程已关闭")
        chat_thread = None


def show_status():
    """显示当前服务器和聊天状态"""
    server_status = "运行中" if server_thread and server_thread.is_alive() else "已停止"
    chat_status = "运行中" if chat_thread and chat_thread.is_alive() else "已停止"
    
    print(f"监听状态: {server_status}")
    print(f"聊天状态: {chat_status}")


def show_help():
    """显示帮助信息"""
    print("可用命令:")
    print("  start resp [port] - 启动anp服务器，可选指定端口号")
    print("  stop resp - 停止anp服务器")
    print("  llm - 启动LLM聊天线程")
    print("  stop llm - 停止LLM聊天线程")
    print("  status - 显示服务器和聊天状态")
    print("  help - 显示此帮助信息")
    print("  test - 测试anp")
    print("  didnew - 创建DID文档和密钥")
    print("  didrun - 选择did启动")
    print("  didstop - 停止did服务")
    print("  didmsg - 发送消息给DID")
    print("  exit - 退出程序")


async def client_notify_chat_thread(message_data: Dict[str, Any]):
    """
    通知聊天线程有新消息
    
    Args:
        message_data: 消息数据
    """
    global client_chat_messages, client_new_message_event
    
    # 添加消息到全局列表
    client_chat_messages.append(message_data)
    
    # 如果列表太长，保留最近的50条消息
    if len(client_chat_messages) > 50:
        client_chat_messages = client_chat_messages[-50:]
    
    # 设置事件，通知聊天线程
    client_new_message_event.set()
    
    # 在控制台显示通知
    logging.info(f"ANP客户请求: {message_data['user_message']}")
    logging.info(f"ANP对方响应: {message_data['assistant_message']}")
   

    # 打印到控制台，确保在聊天线程中可见
    print(f"\nANP-req从本地发出: {message_data['user_message']}")
    print(f"\nANP-req从@{settings.TARGET_SERVER_PORT}收到:  {message_data['assistant_message']}\n")
    
    # 重置事件，为下一次通知做准备
    client_new_message_event.clear()


def chat_to_ANP(custom_msg, token=None, unique_id_arg=None):
    """发送消息到目标服务器（非阻塞方式）
    
    Args:
        custom_msg: 要发送的消息
        token: 认证令牌，如果为None则会启动客户端认证获取token
        unique_id_arg: 可选的唯一ID，用于客户端认证
    """
    # 使用线程模式运行，避免事件循环问题
    thread = threading.Thread(
        target=_chat_to_ANP_thread,
        args=(custom_msg, token, unique_id_arg),
        daemon=True
    )
    thread.start()
    return True

def _chat_to_ANP_thread(custom_msg, token=None, unique_id_arg=None):
    """在线程中运行异步函数的同步包装器"""
    # 创建新的事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # 运行异步函数
        loop.run_until_complete(_chat_to_ANP_impl(custom_msg, token, unique_id_arg))
    except Exception as e:
        logging.error(f"线程中运行_chat_to_ANP_impl时出错: {e}")
        print(f"发送消息时出错: {e}")
    finally:
        # 关闭事件循环
        loop.close()

async def _chat_to_ANP_impl(custom_msg, token=None, unique_id_arg=None):
    """发送消息的实际实现（内部函数）
    """
    try:

       
        target_host = settings.TARGET_SERVER_HOST
        target_port = settings.TARGET_SERVER_PORT
        if os.environ.get('target-port'):
            target_port = os.environ.get('target-port')
        if os.environ.get('target-host'):
            target_host = os.environ.get('target-host')
        base_url = f"http://{target_host}:{target_port}"
        
        if not token:
            print(f"无token，正在启动客户端认证获取token...\n并发送消息: {custom_msg}")
            await ANP_req_auth(unique_id=unique_id_arg, msg=custom_msg)
            token = os.environ.get('did-token', None)
            
        print(f"使用token...\n发送消息: {custom_msg}")
        # 调用did_core中的send_message_to_chat函数
        status, response = await ANP_req_chat(base_url=base_url, silent=True, from_chat=True, msg=custom_msg, token=token)
        
        # 通知聊天线程有新消息
        if status:
            await client_notify_chat_thread({
                "type": "anp_nlp",  # 确保类型与run_chat中的处理逻辑匹配
                "user_message": custom_msg,
                "assistant_message": response.get('answer', '[无回复]') if isinstance(response, dict) else str(response),
                "status": "success"
            })
        else:
            await client_notify_chat_thread({
                "type": "anp_nlp",
                "status": "error",
                "message": f"发送消息失败: {response}"
            })
    except Exception as e:
        logging.error(f"发送消息时出错: {e}")
        print(f"发送消息时出错: {e}")
        # 通知聊天线程出错
        try:
            await client_notify_chat_thread({
                "type": "anp_nlp",
                "status": "error",
                "message": f"发送消息时出错: {e}"
            })
        except Exception:
            pass

def anp_test():
    """测试函数，用于顺序测试服务器启动、消息发送和服务器停止
    
    按顺序执行以下操作并打印日志：
    1. resp_start - 启动服务器
    2. 发送消息 - 发送测试消息
    3. resp_stop - 停止服务器
    """
    import time
    import logging
    import os
    
    # 设置日志级别
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    

    # 1. 启动服务器
    logger.info("===== 步骤1: 启动服务器 =====")
    server_result = resp_start()
    logger.info(f"服务器启动结果: {server_result}")
    logger.info("等待服务器完全启动...")
    time.sleep(3)  # 等待服务器完全启动
        
    # 2. 发送消息
    logger.info("\n===== 步骤2: 发送消息 =====")

        # 检查是否安装了httpx模块
    import httpx
    test_message = "这是一条测试消息，请回复"  
    logger.info(f"发送消息: {test_message}")
    # 获取token，如果环境变量中不存在则使用None
    token = os.environ.get('did-token', None)

    user_list, name_to_dir = get_userdid_list()



    status, did_dict, selected_name = get_user_did(1,user_list,name_to_dir)

    sender = {
        "status": status, 
        "did_dict": did_dict, 
        "name": selected_name,
        "user_dir":name_to_dir[selected_name]
        }

    status, did_dict, selected_name = get_user_did(2,user_list,name_to_dir)
    targeter = {
        "status": status, 
        "did_dict": did_dict, 
        "name": selected_name,
        "user_dir":name_to_dir[selected_name]
        }




    userdid_hostname = dynamic_config.get('web_anp_llmapp.user-did-hostname')
    userdid_port = dynamic_config.get('web_anp_llmapp.user-did-port')

    key_id = "key-1"
    userdid_filepath = dynamic_config.get('web_anp_llmapp.user-did-path')
    user_dir = sender["user_dir"]
    userdid_filepath = os.path.join(userdid_filepath, user_dir)
    did_document_path = f"{userdid_filepath}/did_document.json"
    private_key_path = f"{userdid_filepath}/{key_id}_private.pem"
    print(f"{user_dir}")
    print(f"{did_document_path}")
    print(f"{private_key_path}")

    auth_client = DIDWbaAuthHeader(
    did_document_path=str(did_document_path),
    private_key_path=str(private_key_path)
    )

    base_url = f"http://{userdid_hostname}:{userdid_port}"
    test_url = f"{base_url}/wba/test"

# 4. 发送带DID WBA认证的请求
    logging.info(f"发送认证请求到 {test_url}")

    resp_did = targeter["did_dict"]
    resp_did = resp_did["id"]
    status, response, token =  asyncio.run( send_authenticated_request(test_url, auth_client , resp_did))
    
    if status != 200:
        logging.error(f"认证失败! 状态: {status}")
        logging.error(f"响应: {response}")
        return
        
    logging.info(f"认证成功! 响应: {response}")
    
    # 5. 如果收到令牌，验证令牌并存储
    if token:
        logging.info("收到访问令牌，尝试用于下一个请求")
        status, response = asyncio.run( send_request_with_token(test_url, token))
        
        if status == 200:
            logging.info(f"令牌认证成功! 保存当前令牌！响应: {response}")
            os.environ['did-token'] = token
            
            user_dir = user_dir.strip("_")[1]
            # 发送消息到聊天接口
            sender_uid = sender["user_dir"]
            targeter_uid= targeter["user_dir"]
            msg = f"我是来自web_anp_llmapp的用户聊天测试消息，发送方为: {sender_uid}，目标为: {targeter_uid}"
            result , response = asyncio.run(chat_to_did(base_url, token, msg, user_dir, False, False))
            print(f"发送消息到聊天接口结果: {result}, 响应: {response}")

            msg = f"我是来自web_anp_llmapp的用户第二条聊天测试消息，发送方为: {sender_uid}，目标为: {targeter_uid}"
            result , response = asyncio.run(chat_to_did(base_url, token, msg, user_dir, False, False))
            print(f"发送消息到聊天接口结果: {result}, 响应: {response}")
        else:
            logging.error(f"令牌认证失败! 状态: {status}")
            logging.error(f"响应: {response}")
            print("\n令牌认证失败，客户端示例完成。")

    else:
        logging.warning("未从服务器收到令牌")
        
    # 3. 停止服务器
    logger.info("\n===== 步骤3: 停止服务器 =====")
    stop_result = resp_stop()
    logger.info(f"服务器停止结果: {stop_result}")
    
    logger.info("\n===== 测试完成 =====")
    




def get_userdid_list():
    """获取用户列表
    
    从anp_core/anp_users目录中读取所有用户的配置文件，提取用户名
    
    Returns:
        tuple: (user_list, name_to_dir) 用户名列表和用户名到目录的映射
    """
    user_dirs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anp_core", "anp_users")
    user_list = []
    name_to_dir = {}
    
    # 遍历用户目录
    for user_dir in os.listdir(user_dirs):
        cfg_path = os.path.join(user_dirs, user_dir, "agent_cfg.yaml")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    cfg = yaml.safe_load(f)
                    if cfg and 'name' in cfg:
                        user_list.append(cfg['name'])
                        name_to_dir[cfg['name']] = user_dir
            except Exception as e:
                print(f"读取配置文件 {cfg_path} 出错: {e}")
    
    return user_list, name_to_dir


def get_user_did(choice, user_list, name_to_dir):
    """根据用户选择加载DID文档
    
    Args:
        choice: 用户选择的序号
        user_list: 用户名列表
        name_to_dir: 用户名到目录的映射
        
    Returns:
        tuple: (status, did_dict, selected_name) 操作状态、DID文档字典和选中的用户名
        status为True表示成功，False表示失败
    """
    user_dirs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anp_core", "anp_users")
    
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(user_list):
            selected_name = user_list[idx]
            user_dir = name_to_dir[selected_name]
            
            # 加载 did_document.json
            did_path = os.path.join(user_dirs, user_dir, "did_document.json")
            if os.path.exists(did_path):
                try:
                    with open(did_path, 'r', encoding='utf-8') as f:
                        did_dict = json.load(f)
                    print(f"已加载用户 {selected_name} 的 DID 文档")
                    print(f"DID: {did_dict['id']}")
                    return True, did_dict, selected_name
                except Exception as e:
                    print(f"加载 DID 文档出错: {e}")
                    return False, None, selected_name
            else:
                print(f"未找到用户 {selected_name} 的 DID 文档")
                return False, None, selected_name
        else:
            print("无效的选择")
            return False, None, None
    except ValueError:
        print("请输入有效的数字")
        return False, None, None

def did_create_user( username):
    """创建DID"""
    from anp_core.agent_connect.authentication.did_wba import create_did_wba_document
    import json
    import os
    
    userdid_filepath = dynamic_config.get('web_anp_llmapp.user-did-path')
    userdid_hostname = dynamic_config.get('web_anp_llmapp.user-did-hostname')
    userdid_port = dynamic_config.get('web_anp_llmapp.user-did-port')

    unique_id = secrets.token_hex(8)
    userdid_filepath = os.path.join(userdid_filepath,f"user_{unique_id}")

    # 用户智能体did基本格式为 did:wba:[host]%3A[port]:wba:user:[unique_id]
    # 含义为当前地址端口下wba/user/[unique_id]目录为用户智能体的根目录
    # 其中[unique_id]为随机生成的8位十六进制字符串
    # 类似 服务智能体did基本格式为 did:wba:[host]%3A[port]:wba:agent[unique_id]

    

    path_segments = ["wba", "user", unique_id]
    agent_description_url = f"http://{userdid_hostname}:{userdid_port}/wba/user{unique_id}/ad.json"
    
    # 调用函数创建DID文档和密钥
    did_document, keys = create_did_wba_document(
        hostname = userdid_hostname,
        port = userdid_port,
        path_segments=path_segments,
        agent_description_url=agent_description_url
    )


    
    # 将DID文档保存到文件
    os.makedirs(userdid_filepath, exist_ok=True)
    with open(f"{userdid_filepath}/did_document.json", "w") as f:
        json.dump(did_document, f, indent=4)
    
    # 将私钥和公钥保存到文件
    for key_id, (private_key_pem, public_key_pem) in keys.items():
        with open(f"{userdid_filepath}/{key_id}_private.pem", "wb") as f:
            f.write(private_key_pem)
        with open(f"{userdid_filepath}/{key_id}_public.pem", "wb") as f:
            f.write(public_key_pem)



    agent_cfg = {
        "name": username,
        "unique_id": unique_id,
        "did": did_document["id"]
    }

    
    os.makedirs(userdid_filepath, exist_ok=True)
    with open(f"{userdid_filepath}/agent_cfg.yaml", "w", encoding='utf-8') as f:
        yaml.dump( agent_cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


    import jwt
    from Crypto.PublicKey import RSA

    # 生成 RSA 密钥对
    private_key = RSA.generate(2048).export_key()
    public_key = RSA.import_key(private_key).publickey().export_key()

    def create_jwt(payload: dict, private_key):
        payload["exp"] = datetime.datetime.utcnow() + datetime.timedelta(hours=1)  # 过期时间
        return jwt.encode(payload, private_key, algorithm="RS256")

    def verify_jwt(token: str, public_key):
        try:
            return jwt.decode(token, public_key, algorithms=["RS256"])
        except jwt.ExpiredSignatureError:
            return {"error": "Token expired"}
        except jwt.InvalidTokenError:
            return {"error": "Invalid token"}
    # 创建 JWT

    testcontent = {"user_id": 123}
    token = create_jwt(testcontent, private_key)
    token = verify_jwt(token, public_key)
    if testcontent["user_id"] == token["user_id"]:
        
        with open(f"{userdid_filepath}/private_key.pem", "wb") as f:
            f.write(private_key)
        with open(f"{userdid_filepath}/public_key.pem", "wb") as f:
            f.write(public_key)
    
    
    print(f"DID创建成功: {did_document['id']}")
    print(f"DID文档已保存到: {userdid_filepath}")
    print(f"密钥已保存到: {userdid_filepath}")
    print(f"用户文件已保存到: {userdid_filepath}")
    print(f"jwt密钥已保存到: {userdid_filepath}")
    return did_document


def did_jwt_generate():
    import jwt
    from Crypto.PublicKey import RSA

    # 生成 RSA 密钥对
    private_key = RSA.generate(2048).export_key()
    public_key = RSA.import_key(private_key).publickey().export_key()

    def create_jwt(payload: dict, private_key):
        payload["exp"] = datetime.datetime.utcnow() + datetime.timedelta(hours=1)  # 过期时间
        return jwt.encode(payload, private_key, algorithm="RS256")

    def verify_jwt(token: str, public_key):
        try:
            return jwt.decode(token, public_key, algorithms=["RS256"])
        except jwt.ExpiredSignatureError:
            return {"error": "Token expired"}
        except jwt.InvalidTokenError:
            return {"error": "Invalid token"}
    # 创建 JWT
    testcontent = {"user_id": 123}
    print(f"原文: {testcontent}")
    token = create_jwt(testcontent, private_key)
    print(f"密文: {token}")
    token = verify_jwt(token, public_key)
    print(f"解密: {token}")
    if testcontent["user_id"] == token["user_id"]:
        print(f"jwt正常: {token}")
    with open(f"private_key.pem", "wb") as f:
        f.write(private_key)
    with open(f"public_key.pem", "wb") as f:
        f.write(public_key)




if __name__ == "__main__":
    """主函数，处理命令行输入"""
    set_log_color_level(logging.INFO)
    
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="DID WBA Example with Server capabilities")
    parser.add_argument("--server", action="store_true", help="Run server at startup", default=False)
    parser.add_argument("--port", type=int, help=f"Server port (default: {settings.PORT})", default=settings.PORT)
    
    args = parser.parse_args()
    
    if args.port != settings.PORT:
        settings.PORT = args.port

    import os
    os.environ["PORT"] = f"{settings.PORT}"
    
    # 根据命令行参数启动服务
    if args.server:
        resp_start()
    
    print("DID WBA 示例程序已启动")
    print("输入'help'查看可用命令，输入'exit'退出程序")
    
    # 主循环，处理用户输入
    while True:
        try:
            # 如果聊天线程正在运行，则等待其退出，不处理命令
            if chat_running:
                # 等待聊天线程退出
                while chat_running:
                    time.sleep(0.5)
                if not chat_running:
                    print("聊天线程已退出，恢复命令行控制。")
            command = input("> ").strip().lower()
            if command == "exit":
                print("正在关闭服务...")
                stop_chat()
                resp_stop()
                break
            elif command == "test":
                anp_test()
            elif command == ("jwt"):
                did_jwt_generate()
            elif command.startswith("didnew"):
                 # 检查是否指定了用户名
                parts = command.split(" ")
                if len(parts) > 1:
                   did_create_user(parts[1])
                else:
                    print("请输入用户名。")
                    continue  # 跳过本轮命令输入
            elif command == "help":
                show_help()
            elif command == "status":
                show_status()
            elif command.startswith("start resp"):
                # 检查是否指定了端口号
                parts = command.split()
                if len(parts) > 2:
                    resp_start(parts[2])
                else:
                    resp_start()
            elif command.startswith("stop resp"):
                resp_stop()
            elif command == "llm":
                start_chat()
                # 阻塞主进程直到 chat_thread 结束，避免输入竞争
                if chat_thread:
                    chat_thread.join()
                print("聊天线程已退出，恢复命令行控制。")
                continue  # 跳过本轮命令输入
            elif command == "stop llm":
                stop_chat()
            elif command == "didrun":
                # 从 anp_core/anp_users 目录读取所有用户的 agent_cfg.yaml 文件
                # 提取 name 字段，让用户选择，然后加载对应的 did_document.json
                user_list, name_to_dir = get_userdid_list()
                
                if not user_list:
                    print("未找到可用的用户配置")
                    continue
                
                # 显示用户列表供选择
                print("可用的用户:")
                for i, name in enumerate(user_list):
                    print(f"{i+1}. {name}")
                
                # 获取用户选择
                choice = input("请选择用户 (输入序号): ")
                
                # 使用 get_user_did 函数加载 DID 文档
                status, did_dict, selected_name = get_user_did(choice, user_list, name_to_dir)
                
                # 如果加载成功，可以在这里添加更多处理逻辑
                if status and did_dict:
                    print(f"选择了did: {selected_name}:{did_dict}")# 这里可以添加更多处理逻辑
                    pass
            else:
                print(f"未知命令: {command}")
                print("输入'help'查看可用命令")
        except KeyboardInterrupt:
            print("\n检测到退出信号，正在关闭...")
            resp_stop()
            break
        except Exception as e:
            print(f"错误: {e}")
    
    print("程序已退出")
    sys.exit(0)