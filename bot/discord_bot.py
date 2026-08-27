import asyncio
import datetime
import logging
import threading

import discord
from discord import app_commands

from . import config, spot_engine as engine
from .spot_exchange import get_price
from .spot_simulator import get_state, manual_add
from .notifier import daily_summary
import bot.notifier as _notifier

log = logging.getLogger("cryptobot")

# ── confirmation view ──────────────────────────────────────────────────────────

class ConfirmView(discord.ui.View):
    def __init__(self, action, label: str):
        super().__init__(timeout=30)
        self.action  = action   # async callable
        self.label   = label
        self.done    = False

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return
        self.done = True
        self.stop()
        await interaction.response.defer()
        result = await asyncio.to_thread(self.action)
        await interaction.followup.send(f"✅ {result}", ephemeral=True)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        self.stop()
        await interaction.response.send_message("Cancelled.", ephemeral=True)

    async def on_timeout(self):
        self.done = True


# ── bot client ─────────────────────────────────────────────────────────────────

class _Client(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=int(config.DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(f"Discord slash commands synced to guild {config.DISCORD_GUILD_ID}")
        else:
            await self.tree.sync()
            log.info("Discord slash commands synced globally (may take up to 1h)")

    async def on_ready(self):
        log.info(f"Discord bot logged in as {self.user}")


_client = _Client()
tree    = _client.tree


# ── helpers ────────────────────────────────────────────────────────────────────

def _pair_choices():
    return [app_commands.Choice(name=p, value=p) for p in config.SPOT_TRADING_PAIRS]


def _status_embed() -> discord.Embed:
    state = get_state()
    color = discord.Color.green() if engine.get_status() == "running" else discord.Color.orange()
    embed = discord.Embed(title="📊 Bot Status", color=color)
    embed.add_field(name="Status",  value=engine.get_status().upper(), inline=True)
    embed.add_field(name="Mode",    value=config.SPOT_MODE.upper(),         inline=True)
    embed.add_field(name="Balance", value=f"€{state.balance:,.2f}",   inline=True)
    embed.add_field(name="PnL",     value=f"€{state.total_pnl:+.2f}", inline=True)
    embed.add_field(name="Trades",  value=str(state.total_trades),    inline=True)
    embed.add_field(name="Fees",    value=f"€{state.total_fees:.4f}", inline=True)
    embed.add_field(name="Last tick", value=engine.get_last_tick() or "—", inline=True)

    for pair, pos in state.positions.items():
        try:
            price = get_price(pair)
            pct   = (price - pos.entry_price) / pos.entry_price * 100
            embed.add_field(
                name=f"📌 {pair}",
                value=(f"Entry €{pos.entry_price:,.2f}\n"
                       f"Now   €{price:,.2f} ({pct:+.1f}%)\n"
                       f"Stop  €{pos.trailing_stop_level():,.2f}"),
                inline=True,
            )
        except Exception:
            pass
    return embed


# ── commands ───────────────────────────────────────────────────────────────────

@tree.command(name="crp_status", description="Show bot status, balance and open positions")
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_status_embed(), ephemeral=True)


@tree.command(name="crp_pause", description="Pause trading (bot keeps running, no new trades)")
async def cmd_pause(interaction: discord.Interaction):
    engine.pause()
    await interaction.response.send_message("⏸ Bot paused.", ephemeral=True)


@tree.command(name="crp_resume", description="Resume trading after a pause")
async def cmd_resume(interaction: discord.Interaction):
    engine.resume()
    await interaction.response.send_message("▶️ Bot resumed.", ephemeral=True)


@tree.command(name="crp_summary", description="Send a performance summary to this channel")
async def cmd_summary(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    state   = get_state()
    prices  = {}
    results = {}
    for pair in config.SPOT_TRADING_PAIRS:
        try:
            prices[pair] = get_price(pair)
        except Exception:
            pass
    await asyncio.to_thread(daily_summary, state, config.SPOT_TRADING_PAIRS, prices, results)
    await interaction.followup.send("📅 Summary sent to channel.", ephemeral=True)


@tree.command(name="crp_config", description="Show current bot configuration")
async def cmd_config(interaction: discord.Interaction):
    embed = discord.Embed(title="⚙️ Configuration", color=discord.Color.blurple())
    embed.add_field(name="Interval",       value=config.SPOT_INTERVAL,                    inline=True)
    embed.add_field(name="Position size",  value=f"{config.SPOT_POSITION_SIZE_PCT*100:.0f}%", inline=True)
    embed.add_field(name="Profit floor",   value=f"{config.SPOT_PROFIT_FLOOR_PCT*100:.0f}%",  inline=True)
    embed.add_field(name="Trailing stop",  value=f"{config.SPOT_TRAILING_STOP_PCT*100:.0f}%", inline=True)
    embed.add_field(name="Take profit",    value=f"{config.SPOT_TAKE_PROFIT_PCT*100:.0f}%",   inline=True)
    embed.add_field(name="DCA drop",       value=f"{config.SPOT_DCA_DROP_PCT*100:.0f}%",      inline=True)
    for pair in config.SPOT_TRADING_PAIRS:
        embed.add_field(
            name=pair,
            value=(f"RSI({config.rsi_period_for(pair)}) "
                   f"buy<{config.rsi_oversold_for(pair)} sell>{config.rsi_overbought_for(pair)}\n"
                   f"min exit {config.min_exit_for(pair)*100:.0f}%"),
            inline=True,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="crp_close", description="Manually close an open position")
@app_commands.describe(pair="Trading pair to close")
@app_commands.choices(pair=_pair_choices())
async def cmd_close(interaction: discord.Interaction, pair: str):
    state = get_state()
    if pair not in state.positions:
        await interaction.response.send_message(f"No open position for {pair}.", ephemeral=True)
        return

    pos   = state.positions[pair]
    price = get_price(pair)
    pct   = (price - pos.entry_price) / pos.entry_price * 100

    def do_close():
        engine.manual_close(pair)
        return f"Closed {pair} @ €{price:,.2f} ({pct:+.1f}%)"

    view = ConfirmView(do_close, f"Close {pair}")
    await interaction.response.send_message(
        f"Close **{pair}** position?\nEntry €{pos.entry_price:,.2f} → now €{price:,.2f} ({pct:+.1f}%)",
        view=view,
        ephemeral=True,
    )


@tree.command(name="crp_buy", description="Manually open a position")
@app_commands.describe(pair="Trading pair to buy", size="Position size as % of balance (default: 75)")
@app_commands.choices(pair=_pair_choices())
async def cmd_buy(interaction: discord.Interaction, pair: str, size: int = 75):
    state = get_state()
    if pair in state.positions:
        await interaction.response.send_message(f"Already in a position for {pair}.", ephemeral=True)
        return
    if not 1 <= size <= 100:
        await interaction.response.send_message("Size must be between 1 and 100.", ephemeral=True)
        return

    price    = get_price(pair)
    size_pct = size / 100
    value    = state.balance * size_pct

    def do_buy():
        engine.manual_buy(pair, size_pct)
        return f"Bought {pair} @ €{price:,.2f} ({size}% = ~€{value:,.2f})"

    view = ConfirmView(do_buy, f"Buy {pair}")
    await interaction.response.send_message(
        f"Buy **{pair}** @ €{price:,.2f} using {size}% of balance (~€{value:,.2f})?",
        view=view,
        ephemeral=True,
    )


@tree.command(name="crp_clear", description="Delete all messages in this channel (keeps pinned messages)")
async def cmd_clear(interaction: discord.Interaction):
    class ClearView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)

        @discord.ui.button(label="✅ Yes, clear channel", style=discord.ButtonStyle.danger)
        async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
            self.stop()
            await inter.response.defer(ephemeral=True)
            channel = inter.channel
            deleted = 0
            cutoff  = discord.utils.utcnow() - datetime.timedelta(days=13, hours=23)
            while True:
                msgs = [m async for m in channel.history(limit=100) if not m.pinned]
                if not msgs:
                    break
                recent = [m for m in msgs if m.created_at > cutoff]
                old    = [m for m in msgs if m.created_at <= cutoff]
                if recent:
                    if len(recent) == 1:
                        await recent[0].delete()
                    else:
                        await channel.delete_messages(recent)
                    deleted += len(recent)
                for m in old:
                    try:
                        await m.delete()
                        deleted += 1
                        await asyncio.sleep(0.75)  # avoid rate limit on individual deletes
                    except Exception:
                        pass
                if not recent and not old:
                    break
            # reset tracked tick message IDs — channel is now empty
            _notifier._tick_ids.clear()
            _notifier._save_ids(_notifier._tick_ids)
            await inter.followup.send(f"🗑️ Deleted {deleted} messages.", ephemeral=True)

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
            self.stop()
            await inter.response.send_message("Cancelled.", ephemeral=True)

    await interaction.response.send_message(
        "⚠️ Delete **all messages** in this channel?",
        view=ClearView(),
        ephemeral=True,
    )


# ── background thread ──────────────────────────────────────────────────────────

def start():
    if not config.DISCORD_BOT_TOKEN:
        log.info("DISCORD_BOT_TOKEN not set — Discord bot disabled")
        return

    def _run():
        asyncio.run(_client.start(config.DISCORD_BOT_TOKEN))

    t = threading.Thread(target=_run, daemon=True, name="discord-bot")
    t.start()
    log.info("Discord bot thread started")
