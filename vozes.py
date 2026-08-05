"""
ESCOLHEDOR DE VOZ
=================

Lista as vozes disponíveis na Fish Audio, deixa você OUVIR cada uma e mostra
o identificador para colar no config.json.

Como usar:
    python vozes.py              procura vozes em português
    python vozes.py jarvis       procura por um termo
    python vozes.py --minhas     só as vozes que você mesmo criou
    python vozes.py --edge       as vozes gratuitas do Edge TTS

Não altera nada sozinho: só mostra e toca.
"""
import asyncio
import io
import json
import os
import sys

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

load_dotenv()

PASTA = os.path.dirname(os.path.abspath(__file__))
FRASE = "Bom dia, senhor. Todos os sistemas estão operacionais."


def config_atual() -> dict:
    """Lê o config.json para marcar qual voz está em uso."""
    caminho = os.path.join(PASTA, "config.json")
    if not os.path.exists(caminho):
        return {}
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def tocar_pcm(amostras: np.ndarray, taxa: int) -> None:
    sd.play(amostras, samplerate=taxa)
    sd.wait()


# --------------------------------------------------------------- Fish Audio
def ouvir_fish(sessao, voz_id: str) -> None:
    import fish_audio_sdk as fish

    print("      gerando...", end="", flush=True)
    try:
        dados = b"".join(sessao.tts(fish.TTSRequest(
            reference_id=voz_id, text=FRASE, format="pcm")))
        amostras = np.frombuffer(dados, dtype=np.int16)
        amostras = np.clip(amostras * 2.5, -32768, 32767).astype(np.int16)
        print(" tocando")
        tocar_pcm(amostras, 44100)
    except Exception as e:
        print(f" falhou: {e}")


def modo_fish(termo: str | None, so_minhas: bool) -> None:
    import fish_audio_sdk as fish

    chave = os.environ.get("FISH_AUDIO_API_KEY")
    if not chave:
        print("FISH_AUDIO_API_KEY não está no .env — sem acesso à Fish Audio.")
        print("Use `python vozes.py --edge` para as vozes gratuitas.")
        return

    sessao = fish.Session(chave)
    em_uso = config_atual().get("voz_fish", "")

    filtros = {"page_size": 20}
    if so_minhas:
        filtros["self_only"] = True
    elif termo:
        filtros["title"] = termo
    else:
        filtros["language"] = "pt"

    try:
        resposta = sessao.list_models(**filtros)
    except Exception as e:
        print(f"Falha ao consultar a Fish Audio: {e}")
        return

    vozes = list(getattr(resposta, "items", []) or [])
    if not vozes:
        print("Nenhuma voz encontrada com esse filtro.")
        if so_minhas:
            print("Você ainda não criou vozes próprias em fish.audio.")
        return

    print(f"\n{len(vozes)} voz(es) encontrada(s):\n")
    for i, v in enumerate(vozes, 1):
        marca = "  <<< EM USO" if v.id == em_uso else ""
        idiomas = ",".join(v.languages or [])
        print(f"  [{i:>2}] {v.title[:40]:<42} {idiomas:<8} "
              f"♥{v.like_count or 0}{marca}")
        print(f"       {v.id}")

    print("\nDigite o número para OUVIR, ou Enter para sair.")
    while True:
        try:
            escolha = input("→ ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not escolha:
            return
        if not escolha.isdigit() or not (1 <= int(escolha) <= len(vozes)):
            print("  número inválido")
            continue

        v = vozes[int(escolha) - 1]
        print(f"  {v.title}")
        ouvir_fish(sessao, v.id)
        print(f'\n  Para usar esta voz, no config.json:')
        print(f'     "voz_fish": "{v.id}"\n')


# ----------------------------------------------------------------- Edge TTS
def modo_edge() -> None:
    import edge_tts
    import miniaudio

    async def listar():
        return [v for v in await edge_tts.list_voices()
                if v["Locale"].startswith("pt-")]

    async def gerar(nome: str) -> bytes:
        c = edge_tts.Communicate(FRASE, nome)
        partes = [e["data"] async for e in c.stream() if e["type"] == "audio"]
        return b"".join(partes)

    vozes = asyncio.run(listar())
    em_uso = config_atual().get("voz_edge", "pt-BR-AntonioNeural")

    print(f"\n{len(vozes)} voz(es) gratuita(s) em português:\n")
    for i, v in enumerate(vozes, 1):
        marca = "  <<< EM USO" if v["ShortName"] == em_uso else ""
        genero = "masculina" if v["Gender"] == "Male" else "feminina"
        print(f"  [{i:>2}] {v['ShortName']:<34} {genero:<10} {v['Locale']}{marca}")

    print("\nDigite o número para OUVIR, ou Enter para sair.")
    while True:
        try:
            escolha = input("→ ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not escolha:
            return
        if not escolha.isdigit() or not (1 <= int(escolha) <= len(vozes)):
            print("  número inválido")
            continue

        v = vozes[int(escolha) - 1]
        print(f"  {v['ShortName']} — gerando...", end="", flush=True)
        try:
            mp3 = asyncio.run(gerar(v["ShortName"]))
            dec = miniaudio.decode(mp3,
                                   output_format=miniaudio.SampleFormat.SIGNED16,
                                   nchannels=1, sample_rate=24000)
            print(" tocando")
            tocar_pcm(np.array(dec.samples, dtype=np.int16), 24000)
        except Exception as e:
            print(f" falhou: {e}")
        print(f'\n  Para usar esta voz, no config.json:')
        print(f'     "voz_edge": "{v["ShortName"]}"\n')


def main() -> None:
    args = [a for a in sys.argv[1:]]

    if "--edge" in args:
        modo_edge()
        return

    so_minhas = "--minhas" in args
    termo = next((a for a in args if not a.startswith("--")), None)
    modo_fish(termo, so_minhas)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(0)
