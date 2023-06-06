import asyncio
import time

from aiobaseclient.exceptions import ServiceUnavailableError, TemporaryError
from izihawa_utils.common import filter_none
from telethon import events
from telethon.errors import rpcerrorlist
from telethon.tl.types import DocumentAttributeFilename
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from library.telegram.base import RequestContext
from library.telegram.common import close_button
from library.telegram.utils import safe_execution
from tgbot.app.exceptions import DownloadError
from tgbot.translations import t
from tgbot.views.telegram.base_holder import BaseHolder
from tgbot.views.telegram.common import recode_base64_to_base36, remove_button
from tgbot.views.telegram.progress_bar import ProgressBar, ProgressBarLostMessageError

from .base import BaseCallbackQueryHandler, LongTask


async def delayed_task(create_task, t):
    try:
        await asyncio.sleep(t)
        task = create_task()
        await task
    except asyncio.CancelledError:
        pass


class DownloadTask(LongTask):
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5.0),
        retry=retry_if_exception_type(TemporaryError),
        reraise=True,
    )
    async def download_document(self, document_holder, progress_bar, request_context, filesize=None):
        request_context.statbox(
            action='do_request',
            cid=document_holder.cid,
        )
        await progress_bar.show_banner()
        collected = bytearray()
        async for chunk in self.application.ipfs_http_client.get_iter(document_holder.cid):
            collected.extend(chunk)
            if progress_bar:
                await progress_bar.callback(len(chunk), filesize)
        return bytes(collected)

    async def long_task(self, request_context: RequestContext):
        throttle_secs = 3.0

        async def _on_fail():
            await self.application.get_telegram_client(request_context.bot_name).send_message(
                request_context.chat['chat_id'],
                t('MAINTENANCE', request_context.chat['language']).format(
                    error_picture_url=self.application.config['application']['error_picture_url']
                ),
                buttons=request_context.personal_buttons()
            )

        telegram_file_id = await self.application.database.get_cached_file(request_context.bot_name, self.document_holder.cid)
        if not telegram_file_id:
            telegram_file_id = self.application.get_telegram_client(request_context.bot_name).get_cached_file_id(self.document_holder.cid)
        if telegram_file_id:
            async with safe_execution(error_log=request_context.error_log):
                await self.send_file(
                    document_holder=self.document_holder,
                    file=telegram_file_id,
                    request_context=request_context,
                )
                request_context.statbox(action='cache_hit')
                return

        async with safe_execution(
            error_log=request_context.error_log,
            on_fail=_on_fail,
        ):
            start_time = time.time()
            filename = self.document_holder.get_filename()
            progress_bar_download = ProgressBar(
                telegram_client=self.application.get_telegram_client(request_context.bot_name),
                request_context=request_context,
                banner=t("LOOKING_AT", request_context.chat['language']),
                header=f'⬇️ {filename}',
                tail_text=t('TRANSMITTED_FROM', request_context.chat['language']),
                source='IPFS',
                throttle_secs=throttle_secs,
                last_call=start_time,
            )
            try:
                file = await self.download_document(
                    document_holder=self.document_holder,
                    progress_bar=progress_bar_download,
                    request_context=request_context,
                    filesize=self.document_holder.filesize
                )
                if file:
                    request_context.statbox(
                        action='downloaded',
                        duration=time.time() - start_time,
                        len=len(file),
                    )
                    progress_bar_upload = ProgressBar(
                        telegram_client=self.application.get_telegram_client(request_context.bot_name),
                        request_context=request_context,
                        message=progress_bar_download.message,
                        banner=t("LOOKING_AT", request_context.chat["language"]),
                        header=f'⬇️ {filename}',
                        tail_text=t('UPLOADED_TO_TELEGRAM', request_context.chat["language"]),
                        throttle_secs=throttle_secs,
                        last_call=progress_bar_download.last_call,
                    )
                    uploaded_message = await self.send_file(
                        document_holder=self.document_holder,
                        file=file,
                        progress_callback=progress_bar_upload.callback,
                        request_context=self.request_context,
                    )
                    await self.application.database.put_cached_file(request_context.bot_name, self.document_holder.cid, uploaded_message.file.id)
                    request_context.statbox(
                        action='uploaded',
                        duration=time.time() - start_time,
                        file_id=uploaded_message.file.id
                    )
                else:
                    request_context.statbox(
                        action='not_found',
                        duration=time.time() - start_time,
                    )
                    await self.respond_not_found(
                        request_context=request_context,
                        document_holder=self.document_holder,
                    )
            except (ServiceUnavailableError, DownloadError):
                await self.external_cancel()
            except ProgressBarLostMessageError:
                self.request_context.statbox(
                    action='user_canceled',
                    duration=time.time() - start_time,
                )
            except asyncio.CancelledError:
                request_context.statbox(action='canceled')
            finally:
                messages = filter_none([progress_bar_download.message])
                if messages:
                    async with safe_execution(error_log=request_context.error_log):
                        await self.application.get_telegram_client(request_context.bot_name).delete_messages(
                            request_context.chat['chat_id'],
                            messages
                        )
                request_context.debug_log(action='deleted_progress_message')

    async def respond_not_found(self, request_context: RequestContext, document_holder):
        return await self.application.get_telegram_client(request_context.bot_name).send_message(
            request_context.chat['chat_id'],
            t("SOURCES_UNAVAILABLE", request_context.chat["language"]).format(
                document=document_holder.doi or document_holder.view_builder(
                    request_context.chat["language"]).add_title(bold=False).build()
            ),
            buttons=request_context.personal_buttons()
        )

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((rpcerrorlist.TimeoutError, ValueError)),
    )
    async def send_file(
        self,
        document_holder,
        file,
        request_context,
        close=False,
        progress_callback=None,
        chat_id=None,
        reply_to=None,
    ):
        buttons = []
        if close:
            buttons += [
                close_button()
            ]
        if not buttons:
            buttons = None
        short_abstract = (
            document_holder.view_builder(request_context.chat["language"])
            .add_short_abstract()
            .add_doi_link(label=True, on_newline=True)
            .build()
        )
        caption = (
            f"{short_abstract}\n"
            f"@{self.application.config['telegram']['related_channel']}"
        )
        if self.application.get_telegram_client(request_context.bot_name):
            message = await self.application.get_telegram_client(request_context.bot_name).send_file(
                attributes=[DocumentAttributeFilename(document_holder.get_filename())],
                buttons=buttons,
                caption=caption,
                entity=chat_id or request_context.chat['chat_id'],
                file=file,
                progress_callback=progress_callback,
                reply_to=reply_to,
            )
            request_context.statbox(action='sent')
            return message


class DownloadHandler(BaseCallbackQueryHandler):
    filter = events.CallbackQuery(pattern='^/d_([A-Za-z0-9_-]+)$')
    is_group_handler = True

    def parse_pattern(self, event: events.ChatAction):
        cid = recode_base64_to_base36(event.pattern_match.group(1).decode())
        return cid

    async def handler(self, event: events.ChatAction, request_context: RequestContext):
        cid = self.parse_pattern(event)
        request_context.add_default_fields(mode='download', cid=cid)
        request_context.statbox(action='get')
        document_holder = BaseHolder.create(await self.get_scored_document(self.bot_config['index_aliases'].split(','), 'cid', cid))
        if self.application.user_manager.has_task(request_context.chat['chat_id'], DownloadTask.task_id_for(document_holder)):
            async with safe_execution(is_logging_enabled=False):
                await event.answer(
                    f'{t("ALREADY_DOWNLOADING", request_context.chat["language"])}',
                )
                await remove_button(event, '⬇️', and_empty_too=True)
                return
        if self.application.user_manager.hit_limits(request_context.chat['chat_id']):
            async with safe_execution(is_logging_enabled=False):
                return await event.answer(
                    f'{t("TOO_MANY_DOWNLOADS", request_context.chat["language"])}',
                )
        await remove_button(event, '⬇️', and_empty_too=True)
        return DownloadTask(
            application=self.application,
            document_holder=document_holder,
            request_context=request_context,
        ).schedule()