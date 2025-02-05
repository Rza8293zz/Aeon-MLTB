from secrets import token_hex

from aiofiles.os import makedirs
from mega import MegaApi

from bot import (
    LOGGER,
    non_queued_dl,
    queue_dict_lock,
    task_dict,
    task_dict_lock,
)
from bot.core.config_manager import Config
from bot.helper.ext_utils.bot_utils import sync_to_async
from bot.helper.ext_utils.links_utils import get_mega_link_type
from bot.helper.ext_utils.task_manager import (
    check_running_tasks,
    stop_duplicate_check,
)
from bot.helper.listeners.mega_listener import (
    AsyncExecutor,
    MegaAppListener,
    mega_login,
    mega_logout,
)
from bot.helper.mirror_leech_utils.status_utils.mega_status import MegaDownloadStatus
from bot.helper.mirror_leech_utils.status_utils.queue_status import QueueStatus
from bot.helper.telegram_helper.message_utils import (
    auto_delete_message,
    delete_links,
    send_message,
    send_status_message,
)


async def add_mega_download(listener, path):
    email = Config.MEGA_EMAIL
    password = Config.MEGA_PASSWORD

    executor = AsyncExecutor()
    api = MegaApi(None, None, None, "Aeon")
    folder_api = None

    mega_listener = MegaAppListener(executor.continue_event, listener)
    api.addListener(mega_listener)

    await mega_login(executor, api, email, password)

    if get_mega_link_type(listener.link) == "file":
        await sync_to_async(executor.do, api.getPublicNode, (listener.link,))
        node = mega_listener.public_node
    else:
        folder_api = MegaApi(None, None, None, "Aeon")
        folder_api.addListener(mega_listener)
        await sync_to_async(executor.do, folder_api.loginToFolder, (listener.link,))
        node = await sync_to_async(folder_api.authorizeNode, mega_listener.node)

    if mega_listener.error:
        mmsg = await send_message(listener.message, str(mega_listener.error))
        await mega_logout(executor, api, folder_api)
        await delete_links(listener.message)
        await auto_delete_message(listener.message, mmsg)
        return

    listener.name = listener.name or node.getName()
    msg, button = await stop_duplicate_check(listener)
    if msg:
        mmsg = await send_message(listener.message, msg, button)
        await mega_logout(executor, api, folder_api)
        await delete_links(listener.message)
        await auto_delete_message(listener.message, mmsg)
        return

    gid = token_hex(4)
    listener.size = api.getSize(node)

    added_to_queue, event = await check_running_tasks(listener)
    if added_to_queue:
        LOGGER.info(f"Added to Queue/Download: {listener.name}")
        async with task_dict_lock:
            task_dict[listener.mid] = QueueStatus(listener, gid, "Dl")
        await listener.on_download_start()
        await send_status_message(listener.message)
        await event.wait()
        async with task_dict_lock:
            if listener.mid not in task_dict:
                await mega_logout(executor, api, folder_api)
                return
        from_queue = True
        LOGGER.info(f"Start Queued Download from Mega: {listener.name}")
    else:
        from_queue = False

    async with task_dict_lock:
        task_dict[listener.mid] = MegaDownloadStatus(
            listener,
            mega_listener,
            gid,
            "dl",
        )
    async with queue_dict_lock:
        non_queued_dl.add(listener.mid)

    if from_queue:
        LOGGER.info(f"Start Queued Download from Mega: {listener.name}")
    else:
        await listener.on_download_start()
        await send_status_message(listener.message)
        LOGGER.info(f"Download from Mega: {listener.name}")

    await makedirs(path, exist_ok=True)
    await sync_to_async(
        executor.do,
        api.startDownload,
        (node, path, listener.name, None, False, None),
    )
    await mega_logout(executor, api, folder_api)
