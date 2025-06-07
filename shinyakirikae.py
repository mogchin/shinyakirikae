import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from typing import Dict, Literal, Set

import discord
from discord.ext import commands
from dotenv import load_dotenv

# (既存のログ設定、TOKEN、JST、ANNOUNCE_CHANNEL_IDS、MESSAGE_ID_FILE、ロール設定、EVENTSはそのまま)
# ---------------- ログ設定 ----------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ---------------- BOT TOKEN ----------------
load_dotenv()
TOKEN: str | None = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("環境変数 DISCORD_BOT_TOKEN が未設定です")

# ---------------- JST ----------------
JST = timezone(timedelta(hours=9), "JST")

# ---------------- アナウンス設定 ----------------
ANNOUNCE_CHANNEL_IDS: Set[int] = {
    1138454372225912832,
    1161282636136857621,
    894511550344331284,
}
MESSAGE_ID_FILE = "announcement_message_ids.json"

# ---------------- ロール設定 ----------------
QUIET_ROLES: Set[int] = {
    1372541940155027631, # QuietロールのID例1
    1373111240515260617, # QuietロールのID例2
}
DEEP_NIGHT_OPTIN_ROLE: int = 870001468864884858
MALE_ROLES: Set[int] = {812663143654096946, 784723518402592805}
FEMALE_ROLES: Set[int] = {812698196098154538, 784723518402592804}
MALE_NIGHT_ROLE: int = 1374870988097061014
FEMALE_NIGHT_ROLE: int = 1374871326027812924

# ---------------- 時刻イベント (23:00-06:00) ----------------
EVENTS = [
    (time(22, 50), "prep_night"),
    (time(23, 0), "start_night"),
    (time(5, 50), "prep_day"),
    (time(6, 0), "end_night"),
]


class NightRoleAssigner(commands.Cog):
    """22:50/05:50 にスナップショットを取り 23:00/06:00 に処理を実行"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._night_targets: Dict[int, Dict[int, int]] = defaultdict(dict)
        self._remove_targets: Dict[int, Set[int]] = defaultdict(set)
        self._announcement_message_ids: Dict[int, int] = {} # チャンネルID: メッセージID
        self._task: asyncio.Task | None = None

    def _load_message_ids(self):
        try:
            with open(MESSAGE_ID_FILE, 'r') as f:
                data = json.load(f)
                # キーを整数に変換
                self._announcement_message_ids = {int(k): v for k, v in data.items()}
                logger.info(f"{MESSAGE_ID_FILE} からメッセージIDをロードしました。")
        except FileNotFoundError:
            logger.info(f"{MESSAGE_ID_FILE} が見つかりません。新規に作成します。")
        except (json.JSONDecodeError, TypeError) as e: # TypeErrorを追加してキー変換エラーも補足
            logger.error(f"{MESSAGE_ID_FILE} の解析またはキーの型変換に失敗しました: {e}")
            self._announcement_message_ids = {} # エラー時は空にする

    def _save_message_ids(self):
        with open(MESSAGE_ID_FILE, 'w') as f:
            json.dump(self._announcement_message_ids, f, indent=4)
        logger.info(f"{MESSAGE_ID_FILE} にメッセージIDを保存しました。")

    async def cog_load(self):
        self._load_message_ids()
        self._task = self.bot.loop.create_task(self._scheduler())
        logger.info("NightRoleAssigner スケジューラを開始しました")

    def cog_unload(self):
        if self._task and not self._task.cancelled():
            self._task.cancel()
            logger.info("NightRoleAssigner スケジューラを停止しました")

    # _scheduler, _run_event メソッドは変更なし

    async def _scheduler(self):
        await self.bot.wait_until_ready()
        logger.info("スケジューラが開始されました")
        while not self.bot.is_closed():
            try:
                now = datetime.now(JST)
                next_dt, evt = self._next_event(now)
                sleep_seconds = (next_dt - now).total_seconds()
                logger.info(f"次のイベント: {evt} at {next_dt.strftime('%Y-%m-%d %H:%M:%S')} ({sleep_seconds:.1f}秒後)")
                await asyncio.sleep(sleep_seconds)
                async with self._lock:
                    for guild in self.bot.guilds:
                        try:
                            await self._run_event(guild, evt)
                        except Exception as e:
                            logger.error(f"ギルド {guild.name} でのイベント {evt} 実行中にエラー: {e}")
            except asyncio.CancelledError:
                logger.info("スケジューラがキャンセルされました")
                break
            except Exception as e:
                logger.error(f"スケジューラでエラーが発生: {e}")
                await asyncio.sleep(60) # エラー発生時は少し待って再試行

    async def _run_event(self, guild: discord.Guild, evt: str):
        logger.info(f"[{guild.name}] イベント実行: {evt}")
        if evt == "prep_night":
            await self._prep_night(guild)
        elif evt == "start_night":
            await self._start_night(guild)
        elif evt == "prep_day":
            await self._prep_day(guild)
        elif evt == "end_night":
            await self._end_night(guild)

    # ---------------- アナウンス更新 (Embed使用) ----------------
    async def _update_announcement(self, guild: discord.Guild, state: Literal["night", "day"]):
        """状態表示メッセージをEmbedで作成または編集し、常に最新にする"""
        embed = discord.Embed(timestamp=datetime.now(JST))

        if state == "night":
            embed.title = "🌙 深夜時間"
            embed.description = (
                f"現在は深夜時間です。\n"
                f"以下のロールのみメンション可能です。\n"
                f"<@&{MALE_NIGHT_ROLE}> <@&{FEMALE_NIGHT_ROLE}>"
            )
            embed.color = discord.Color.dark_purple()

        else:  # day
            embed.title = "☀️ 通常時間"
            embed.description = "現在は通常時間です。"
            embed.color = discord.Color.orange()


        for channel_id in ANNOUNCE_CHANNEL_IDS:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                logger.warning(f"アナウンス用チャンネル(ID:{channel_id})が見つからないか、テキストチャンネルではありません。")
                continue

            # 以前のメッセージがあれば削除
            old_message_id = self._announcement_message_ids.get(channel_id)
            if old_message_id:
                try:
                    message_to_delete = await channel.fetch_message(old_message_id)
                    await message_to_delete.delete()
                    logger.info(f"チャンネル {channel.name} の古いアナウンスメッセージ(ID:{old_message_id})を削除しました。")
                except discord.NotFound:
                    logger.info(f"削除対象のメッセージ(ID:{old_message_id})が見つかりませんでした。")
                except discord.Forbidden:
                    logger.error(f"チャンネル {channel.name} のメッセージ削除権限がありません。")
                except discord.HTTPException as e:
                    logger.error(f"古いメッセージ削除中にHTTPエラー: {e}")
                finally:
                    # 削除試行後は、成功失敗に関わらずIDを管理対象から外す（次回エラーを防ぐため）
                    self._announcement_message_ids.pop(channel_id, None)


            # 新しいメッセージを送信
            try:
                new_message = await channel.send(embed=embed)
                self._announcement_message_ids[channel_id] = new_message.id
                logger.info(f"チャンネル {channel.name} に新しいアナウンスメッセージ(ID:{new_message.id})を投稿しました。")
            except discord.Forbidden:
                logger.error(f"チャンネル {channel.name} へのメッセージ投稿権限がありません。")
            except discord.HTTPException as e:
                logger.error(f"アナウンスメッセージ投稿中にHTTPエラー: {e}")

        self._save_message_ids() # メッセージIDリストの変更を保存

    # ----- 22:50 ロールスナップショット -----
    async def _prep_night(self, guild: discord.Guild):
        opt_role = guild.get_role(DEEP_NIGHT_OPTIN_ROLE)
        targets: Dict[int, int] = {}
        if opt_role:
            logger.info(f"[{guild.name}] 深夜オプトイン対象者: {len(opt_role.members)}人")
            for m in opt_role.members:
                roles = {r.id for r in m.roles}
                if roles & MALE_ROLES:
                    targets[m.id] = MALE_NIGHT_ROLE
                elif roles & FEMALE_ROLES:
                    targets[m.id] = FEMALE_NIGHT_ROLE
        else:
            logger.warning(f"[{guild.name}] 深夜オプトインロールが見つかりません (ID: {DEEP_NIGHT_OPTIN_ROLE})")
        self._night_targets[guild.id] = targets
        logger.info(f"[{guild.name}] 夜ロール付与対象: {len(targets)}人")

    # ----- 23:00 mentionable OFF & 深夜ロール付与 & アナウンス -----
    async def _start_night(self, guild: discord.Guild):
        # 1. アナウンスを更新 (Embedを使用し、常に最新に)
        await self._update_announcement(guild, state="night")
        # 2. Quietロールのメンション可否を設定
        await self._set_quiet_roles(guild, mentionable=False)
        # 3. 深夜ロールを付与
        targets = self._night_targets.get(guild.id, {})
        if not targets:
            logger.info(f"[{guild.name}] _start_night: 付与対象者なし。")
            return

        male_night_role = guild.get_role(MALE_NIGHT_ROLE)
        female_night_role = guild.get_role(FEMALE_NIGHT_ROLE)
        added_count = 0
        tasks_ = []

        for mid, rid_to_add in targets.items():
            member = guild.get_member(mid)
            if not member:
                logger.debug(f"[{guild.name}] _start_night: メンバー(ID:{mid})が見つかりません。")
                continue

            role_obj_to_add = None
            if rid_to_add == MALE_NIGHT_ROLE:
                role_obj_to_add = male_night_role
            elif rid_to_add == FEMALE_NIGHT_ROLE:
                role_obj_to_add = female_night_role

            if role_obj_to_add and role_obj_to_add not in member.roles:
                tasks_.append(member.add_roles(role_obj_to_add, reason="NightRoleAssigner: add night role"))
                added_count +=1
            elif role_obj_to_add and role_obj_to_add in member.roles:
                 logger.debug(f"[{guild.name}] _start_night: メンバー {member.display_name} は既にロール {role_obj_to_add.name} を持っています。")


        if tasks_:
            results = await asyncio.gather(*tasks_, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    # tasks_ と targets の関連付けが直接できないため、詳細なエラー特定は難しい
                    logger.error(f"[{guild.name}] _start_night: ロール付与中にエラーが発生: {result} (タスク {i+1}/{len(tasks_)})")
        logger.info(f"[{guild.name}] _start_night: {added_count}人に深夜ロールを付与試行。")
        self._night_targets.pop(guild.id, None) # 処理が終わったのでクリア


    # ----- 05:50 剥奪対象スナップショット -----
    async def _prep_day(self, guild: discord.Guild):
        male_night = guild.get_role(MALE_NIGHT_ROLE)
        female_night = guild.get_role(FEMALE_NIGHT_ROLE)
        targets: Set[int] = set()
        if male_night:
            targets.update({m.id for m in male_night.members})
        if female_night:
            targets.update({m.id for m in female_night.members})
        self._remove_targets[guild.id] = targets
        logger.info(f"[{guild.name}] 夜ロール剥奪対象: {len(targets)}人")

    # ----- 06:00 mentionable ON & 深夜ロール剥奪 & アナウンス -----
    async def _end_night(self, guild: discord.Guild):
        # 1. アナウンスを更新 (Embedを使用し、常に最新に)
        await self._update_announcement(guild, state="day")
        # 2. Quietロールのメンション可否を設定
        await self._set_quiet_roles(guild, mentionable=True)
        # 3. 深夜ロールを剥奪
        targets = self._remove_targets.get(guild.id, set())
        if not targets:
            logger.info(f"[{guild.name}] _end_night: 剥奪対象者なし。")
            return

        male_night_role = guild.get_role(MALE_NIGHT_ROLE)
        female_night_role = guild.get_role(FEMALE_NIGHT_ROLE)
        removed_count = 0
        tasks_ = []

        for mid in targets:
            member = guild.get_member(mid)
            if not member:
                logger.debug(f"[{guild.name}] _end_night: メンバー(ID:{mid})が見つかりません。")
                continue

            roles_to_remove = []
            if male_night_role and male_night_role in member.roles:
                roles_to_remove.append(male_night_role)
            if female_night_role and female_night_role in member.roles:
                roles_to_remove.append(female_night_role)

            for role_obj in roles_to_remove:
                 tasks_.append(member.remove_roles(role_obj, reason="NightRoleAssigner: remove night role"))
                 removed_count += 1


        if tasks_:
            results = await asyncio.gather(*tasks_, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"[{guild.name}] _end_night: ロール剥奪中にエラーが発生: {result} (タスク {i+1}/{len(tasks_)})")
        logger.info(f"[{guild.name}] _end_night: {removed_count}件の深夜ロールを剥奪試行。")
        self._remove_targets.pop(guild.id, None) # 処理が終わったのでクリア

    # ---------------- mentionable 切替 ----------------
    async def _set_quiet_roles(self, guild: discord.Guild, mentionable: bool):
        tasks_ = []
        changed_count = 0
        for rid in QUIET_ROLES:
            role = guild.get_role(rid)
            if role and role.mentionable != mentionable:
                try:
                    tasks_.append(role.edit(mentionable=mentionable, reason=f"NightRoleAssigner: toggle mentionable to {mentionable}"))
                    changed_count +=1
                except discord.Forbidden:
                    logger.error(f"[{guild.name}] ロール {role.name}(ID:{rid}) のメンション設定変更権限がありません。")
                except discord.HTTPException as e:
                    logger.error(f"[{guild.name}] ロール {role.name}(ID:{rid}) のメンション設定変更中にHTTPエラー: {e}")
            elif not role:
                 logger.warning(f"[{guild.name}] Quietロール(ID:{rid})が見つかりません。")


        if tasks_:
            results = await asyncio.gather(*tasks_, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                     logger.error(f"[{guild.name}] _set_quiet_roles: ロール編集中にエラー: {result} (タスク {i+1}/{len(tasks_)})")
        if changed_count > 0:
            logger.info(f"[{guild.name}] {changed_count}個のQuietロールのmentionableを{mentionable}に設定しました。")
        else:
            logger.info(f"[{guild.name}] Quietロールのmentionable設定に変更はありませんでした ({'既に' if mentionable else '既に非'}メンション可)。")


    # ---------------- 次イベント計算 ----------------
    @staticmethod
    def _next_event(now: datetime) -> tuple[datetime, str]:
        today = now.date()
        upcoming: list[tuple[datetime, str]] = []
        for t, name in EVENTS:
            dt = datetime.combine(today, t, JST)
            if dt <= now: # 現在時刻より前か同じ場合は翌日の同時刻
                dt += timedelta(days=1)
            upcoming.append((dt, name))
        # 最も近い未来のイベントを選択
        return min(upcoming, key=lambda x: x[0])


    # ---------------- デバッグコマンド (現状は機能しないまま)----------------
    @commands.command(name="night_status")
    @commands.has_permissions(administrator=True)
    async def night_status(self, ctx):
        # このコマンドは現状、具体的な状態表示機能は持ちません。
        # 必要であれば、現在の内部状態（次のイベント時刻など）を表示する機能を追加できます。
        next_event_dt, next_event_name = self._next_event(datetime.now(JST))
        embed = discord.Embed(title="NightRoleAssigner Status", color=discord.Color.blue(), timestamp=datetime.now(JST))
        embed.add_field(name="Scheduler Task", value="Running" if self._task and not self._task.done() else "Not Running or Done", inline=False)
        embed.add_field(name="Next Event", value=f"`{next_event_name}` at `{next_event_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}`", inline=False)

        # アナウンスメッセージIDの状況
        if self._announcement_message_ids:
            msg_ids_str = "\n".join(f"Channel <#{ch_id}>: Message ID `{msg_id}`" for ch_id, msg_id in self._announcement_message_ids.items())
            embed.add_field(name="Last Announcement IDs", value=msg_ids_str, inline=False)
        else:
            embed.add_field(name="Last Announcement IDs", value="No announcement messages tracked.", inline=False)

        # 現在のスナップショット情報（デバッグ用）
        # guild_id = ctx.guild.id if ctx.guild else None # コマンドがギルド内で使われた場合
        # if guild_id:
        #     night_targets_count = len(self._night_targets.get(guild_id, {}))
        #     remove_targets_count = len(self._remove_targets.get(guild_id, set()))
        #     embed.add_field(name=f"Snapshot for Guild {guild_id}",
        #                     value=f"Night Targets: {night_targets_count}\nRemove Targets: {remove_targets_count}",
        #                     inline=False)

        await ctx.send(embed=embed)


# ---------------- Bot 起動 ----------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True # メンバーのロール変更や取得に必要
# intents.message_content = True # コマンドプレフィックスに必要。スラッシュコマンドなら不要な場合もある

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info(f"discord.py version: {discord.__version__}")
    logger.info(f"ギルド数: {len(bot.guilds)}")
    for guild in bot.guilds:
        logger.info(f"  - {guild.name} (ID: {guild.id})")


async def setup_hook():
    await bot.add_cog(NightRoleAssigner(bot))
    logger.info("NightRoleAssigner Cog を追加しました")

bot.setup_hook = setup_hook

if __name__ == "__main__":
    if TOKEN is None:
        logger.critical("DISCORD_BOT_TOKEN が設定されていません。Botを起動できません。")
    else:
        try:
            bot.run(TOKEN)
        except discord.LoginFailure:
            logger.critical("Botトークンが不正です。Botを起動できませんでした。")
        except KeyboardInterrupt:
            logger.info("Botを手動で停止しています...")
        except Exception as e:
            logger.error(f"Bot実行中に予期せぬエラーが発生: {e}", exc_info=True)
