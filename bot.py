import os
import asyncio
from dataclasses import dataclass
from typing import Deque, Dict, Optional, List

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
from collections import deque

# =========================
# Config & Environment
# =========================

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in the environment variables or .env file.")

# FFmpeg executable (default assumes ffmpeg is in PATH)
# Em produção (Railway, etc.), você instala o ffmpeg no container e deixa só "ffmpeg"
FFMPEG_EXECUTABLE = os.getenv("FFMPEG_EXECUTABLE", "ffmpeg")

# =========================
# Data Models
# =========================

@dataclass
class Track:
    title: str
    stream_url: str
    webpage_url: str
    duration: Optional[int]  # in seconds
    requester_id: int


@dataclass
class GuildMusicState:
    queue: Deque[Track]
    now_playing: Optional[Track]
    voice_client: Optional[discord.VoiceClient]

    def __init__(self) -> None:
        self.queue = deque()
        self.now_playing = None
        self.voice_client = None


# =========================
# Bot & Intents
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True  # importante para voz
# message_content não é necessário para slash commands

bot = commands.Bot(command_prefix="!", intents=intents)
music_states: Dict[int, GuildMusicState] = {}


def get_music_state(guild_id: int) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState()
    return music_states[guild_id]


# =========================
# Helper: yt-dlp search / extraction
# =========================

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}


async def ytdlp_extract(query: str) -> Optional[Dict]:
    """
    Executa o yt-dlp em uma thread separada para não travar o event loop.
    Aceita URL direta ou texto de busca.
    """
    loop = asyncio.get_running_loop()

    def _extract() -> Optional[Dict]:
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            try:
                return ydl.extract_info(query, download=False)
            except Exception as e:
                print(f"[yt-dlp] Error extracting info for query '{query}': {e}")
                return None

    return await loop.run_in_executor(None, _extract)


def build_track_from_info(info: Dict, requester_id: int) -> Track:
    # Se for resultado de busca, pega a primeira entrada
    if "entries" in info:
        info = info["entries"][0]

    title = info.get("title", "Untitled")
    stream_url = info.get("url")
    webpage_url = info.get("webpage_url", info.get("original_url", ""))
    duration = info.get("duration")

    return Track(
        title=title,
        stream_url=stream_url,
        webpage_url=webpage_url,
        duration=duration,
        requester_id=requester_id,
    )


# =========================
# Helper: Voice / Playback
# =========================

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


async def ensure_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    """
    Garante que o bot está conectado no mesmo canal de voz que o usuário.
    Se não estiver conectado, conecta.
    Se estiver em outro canal, move.
    Retorna o VoiceClient ou None se o user não estiver em voz.
    """
    guild = interaction.guild
    assert guild is not None

    voice_state = interaction.user.voice  # type: ignore[attr-defined]
    if voice_state is None or voice_state.channel is None:
        await interaction.followup.send("Você precisa estar em um canal de voz primeiro.", ephemeral=True)
        return None

    target_channel = voice_state.channel
    state = get_music_state(guild.id)
    voice_client = state.voice_client

    if voice_client is None or not voice_client.is_connected():
        voice_client = await target_channel.connect()
        state.voice_client = voice_client
    elif voice_client.channel != target_channel:
        await voice_client.move_to(target_channel)

    return voice_client


async def start_playback(guild: discord.Guild) -> None:
    """
    Inicia a reprodução da próxima música na fila da guild.
    Chamado quando:
      - uma nova música entra na fila e nada está tocando
      - a música anterior termina
    """
    state = get_music_state(guild.id)
    vc = state.voice_client

    if vc is None or not vc.is_connected():
        state.now_playing = None
        state.queue.clear()
        return

    if not state.queue:
        state.now_playing = None
        # Opcional: desconectar após alguns segundos sem fila
        await asyncio.sleep(5)
        if not vc.is_playing() and not state.queue:
            await vc.disconnect()
            state.voice_client = None
        return

    # Pega próxima música
    track = state.queue.popleft()
    state.now_playing = track

    def after_playback(error: Optional[Exception]) -> None:
        if error:
            print(f"[Playback] Error while playing track '{track.title}': {error}")
        # Agenda a próxima música no event loop
        coro = start_playback(guild)
        fut = asyncio.run_coroutine_threadsafe(coro, bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"[Playback] Error scheduling next track: {e}")

    try:
        source = discord.FFmpegOpusAudio(
            track.stream_url,
            executable=FFMPEG_EXECUTABLE,
            **FFMPEG_OPTIONS,
        )
    except Exception as e:
        print(f"[FFmpeg] Failed to create audio source for '{track.title}': {e}")
        # Tenta avançar para a próxima da fila
        await start_playback(guild)
        return

    vc.play(source, after=after_playback)
    print(f"[Playback] Now playing in guild {guild.name} ({guild.id}): {track.title}")


# =========================
# Events
# =========================

@bot.event
async def on_ready():
    # Sincroniza os slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} application commands.")
    except Exception as e:
        print(f"Error syncing commands: {e}")

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")


# =========================
# Slash Commands
# =========================

@bot.tree.command(name="join", description="Faz o bot entrar no seu canal de voz.")
async def join(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return

    state = get_music_state(guild.id)
    voice_state = interaction.user.voice  # type: ignore[attr-defined]
    if voice_state is None or voice_state.channel is None:
        await interaction.followup.send("Você precisa estar em um canal de voz primeiro.", ephemeral=True)
        return

    channel = voice_state.channel

    if state.voice_client is None or not state.voice_client.is_connected():
        state.voice_client = await channel.connect()
        await interaction.followup.send(f"Entrei em **{channel.name}**.", ephemeral=True)
    else:
        await state.voice_client.move_to(channel)
        await interaction.followup.send(f"Mudei para **{channel.name}**.", ephemeral=True)


@bot.tree.command(name="leave", description="Faz o bot sair do canal de voz e limpa a fila.")
async def leave(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return

    state = get_music_state(guild.id)
    vc = state.voice_client

    if vc is None or not vc.is_connected():
        await interaction.followup.send("Eu não estou em nenhum canal de voz.", ephemeral=True)
        return

    state.queue.clear()
    state.now_playing = None
    await vc.disconnect()
    state.voice_client = None

    await interaction.followup.send("Saí do canal de voz e limpei a fila.", ephemeral=True)


@bot.tree.command(name="play", description="Reproduz uma música ou adiciona à fila.")
@app_commands.describe(query="Link ou nome da música")
async def play(interaction: discord.Interaction, query: str):
    # Resposta não-efêmera: todo mundo no canal vê o comando
    await interaction.response.defer()

    guild = interaction.guild
    if guild is None:
        return

    state = get_music_state(guild.id)

    # Garante que o bot está no canal de voz certo
    vc = await ensure_voice(interaction)
    if vc is None:
        return

    # Extrai info com yt-dlp (URL direta ou pesquisa)
    info = await ytdlp_extract(query)
    if info is None:
        await interaction.followup.send(
            "Não consegui encontrar ou tocar essa música. Tente outro link ou outro nome.",
            ephemeral=True,
        )
        return

    # Cria Track
    track = build_track_from_info(info, requester_id=interaction.user.id)
    if not track.stream_url:
        await interaction.followup.send(
            "Não foi possível obter o stream de áudio dessa faixa.",
            ephemeral=True,
        )
        return

    state.queue.append(track)

    # Se nada está tocando, iniciar playback
    if not vc.is_playing() and not vc.is_paused():
        await interaction.followup.send(f"Tocando agora: **{track.title}**")
        await start_playback(guild)
    else:
        await interaction.followup.send(f"Adicionado à fila: **{track.title}**")


@bot.tree.command(name="skip", description="Pula a música atual.")
async def skip(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return

    state = get_music_state(guild.id)
    vc = state.voice_client

    if vc is None or not vc.is_connected():
        await interaction.followup.send("Eu não estou em nenhum canal de voz.", ephemeral=True)
        return

    if not vc.is_playing() and not vc.is_paused():
        await interaction.followup.send("Não há nenhuma música tocando agora.", ephemeral=True)
        return

    vc.stop()
    await interaction.followup.send("Pulei a música atual.", ephemeral=True)

@bot.tree.command(name="zeze", description="?")
async def skip(interaction: discord.Interaction):
    await interaction.followup.send("Hello zeze, how are you my friend? good morning", ephemeral=True)

@bot.tree.command(name="pause", description="Pausa a música atual.")
async def pause(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return

    state = get_music_state(guild.id)
    vc = state.voice_client

    if vc is None or not vc.is_connected():
        await interaction.followup.send("Eu não estou em nenhum canal de voz.", ephemeral=True)
        return

    if not vc.is_playing():
        await interaction.followup.send("Não há nenhuma música tocando para pausar.", ephemeral=True)
        return

    vc.pause()
    await interaction.followup.send("Música pausada.", ephemeral=True)


@bot.tree.command(name="resume", description="Retoma a música pausada.")
async def resume(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return

    state = get_music_state(guild.id)
    vc = state.voice_client

    if vc is None or not vc.is_connected():
        await interaction.followup.send("Eu não estou em nenhum canal de voz.", ephemeral=True)
        return

    if not vc.is_paused():
        await interaction.followup.send("A música não está pausada.", ephemeral=True)
        return

    vc.resume()
    await interaction.followup.send("Música retomada.", ephemeral=True)


@bot.tree.command(name="stop", description="Para a música e limpa a fila.")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return

    state = get_music_state(guild.id)
    vc = state.voice_client

    if vc is None or not vc.is_connected():
        await interaction.followup.send("Eu não estou em nenhum canal de voz.", ephemeral=True)
        return

    state.queue.clear()
    state.now_playing = None
    if vc.is_playing() or vc.is_paused():
        vc.stop()

    await interaction.followup.send("Parei a reprodução e limpei a fila.", ephemeral=True)


@bot.tree.command(name="nowplaying", description="Mostra a música que está tocando agora.")
async def now_playing(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return

    state = get_music_state(guild.id)
    track = state.now_playing

    if track is None:
        await interaction.followup.send("Não há nenhuma música tocando no momento.", ephemeral=True)
        return

    duration_str = "desconhecida"
    if track.duration is not None:
        minutes, seconds = divmod(track.duration, 60)
        duration_str = f"{minutes:02d}:{seconds:02d}"

    desc = f"**{track.title}**\nDuração: `{duration_str}`"
    if track.webpage_url:
        desc += f"\nLink: {track.webpage_url}"

    await interaction.followup.send(desc, ephemeral=True)


@bot.tree.command(name="queue", description="Mostra as próximas músicas da fila.")
async def queue_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return

    state = get_music_state(guild.id)
    if not state.queue:
        await interaction.followup.send("A fila está vazia.", ephemeral=True)
        return

    lines: List[str] = []
    for idx, track in enumerate(list(state.queue)[:10], start=1):
        if track.duration:
            minutes, seconds = divmod(track.duration, 60)
            dur = f"{minutes:02d}:{seconds:02d}"
        else:
            dur = "??:??"
        lines.append(f"`{idx:02d}.` **{track.title}** (`{dur}`)")

    msg = "Próximas músicas na fila:\n" + "\n".join(lines)
    await interaction.followup.send(msg, ephemeral=True)


# =========================
# Run
# =========================

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)