"""
ESCOLHEDOR DE VOZ (Fish Audio)
==============================

Navega pelo catálogo de vozes da Fish Audio, deixa você OUVIR cada uma e
grava a escolhida direto no config.json.

Como usar:
    python vozes.py                 vozes em português, mais usadas primeiro
    python vozes.py --novas         as mais recentes
    python vozes.py --minhas        só as vozes que você mesmo clonou
    python vozes.py --edge          as vozes gratuitas do Edge TTS (reserva)

Dentro do programa:
    5           ouve a voz número 5
    s 5         salva a voz 5 no config.json
    n / p       página seguinte / anterior
    b <termo>   busca por nome
    q           sai
"""
import asyncio
import json
import os
import sys

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

load_dotenv()

PASTA = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PASTA, "config.json")
FRASE = "Bom dia, senhor. Todos os sistemas estão operacionais."
POR_PAGINA = 15


# ------------------------------------------------------------------ config
def ler_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"[AVISO] config.json inválido ({e}) — não vou poder salvar.")
        return {}


def salvar_voz(chave: str, valor: str, titulo: str) -> None:
    """Grava a voz escolhida no config.json, preservando o resto."""
    if not os.path.exists(CONFIG_PATH):
        print("  config.json não existe. Copie o config.example.json primeiro.")
        return

    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  config.json está com JSON inválido ({e}). Corrija antes.")
        return

    anterior = cfg.get(chave, "(nenhuma)")
    cfg[chave] = valor

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"\n  Salvo em config.json:")
    print(f"     {chave}: {anterior}")
    print(f"          -> {valor}   ({titulo})")
    print("  Reinicie o assistente para a voz nova valer.\n")


def tocar(amostras: np.ndarray, taxa: int) -> None:
    sd.play(amostras, samplerate=taxa)
    sd.wait()


# -------------------------------------------------------------- Fish Audio
def ouvir_fish(sessao, voz_id: str) -> None:
    import fish_audio_sdk as fish

    print("      gerando...", end="", flush=True)
    try:
        dados = b"".join(sessao.tts(fish.TTSRequest(
            reference_id=voz_id, text=FRASE, format="pcm")))
        amostras = np.frombuffer(dados, dtype=np.int16)
        amostras = np.clip(amostras * 2.5, -32768, 32767).astype(np.int16)
        print(" tocando")
        tocar(amostras, 44100)
    except Exception as e:
        print(f" falhou: {e}")


def mostrar_pagina(vozes, pagina, total, em_uso) -> None:
    ultima = (total + POR_PAGINA - 1) // POR_PAGINA
    print(f"\n  página {pagina} de {ultima}   ({total} vozes no total)\n")
    for i, v in enumerate(vozes, 1):
        marca = "  <<< EM USO" if v.id == em_uso else ""
        idiomas = ",".join(v.languages or [])
        print(f"  [{i:>2}] {(v.title or '?')[:44]:<46} {idiomas:<7} "
              f"usos {v.task_count or 0:<7}{marca}")


def modo_fish(sort_by: str, so_minhas: bool) -> None:
    import fish_audio_sdk as fish

    chave = os.environ.get("FISH_AUDIO_API_KEY")
    if not chave:
        print("FISH_AUDIO_API_KEY não está no .env.")
        print("Use `python vozes.py --edge` para as vozes gratuitas.")
        return

    sessao = fish.Session(chave)
    em_uso = ler_config().get("voz_fish", "")

    pagina, busca = 1, None

    while True:
        filtros = {"page_size": POR_PAGINA, "page_number": pagina,
                   "sort_by": sort_by}
        if so_minhas:
            filtros["self_only"] = True
        elif busca:
            filtros["title"] = busca
        else:
            filtros["language"] = "pt"

        try:
            resposta = sessao.list_models(**filtros)
        except Exception as e:
            print(f"Falha ao consultar a Fish Audio: {e}")
            return

        vozes = list(resposta.items or [])
        total = resposta.total or 0

        if not vozes:
            print("\n  Nenhuma voz nesta página." if pagina > 1
                  else "\n  Nenhuma voz encontrada.")
            if so_minhas:
                print("  Você ainda não clonou nenhuma voz em fish.audio.")
                return
            if pagina > 1:
                pagina -= 1
                continue
            return

        titulo = ("suas vozes" if so_minhas
                  else f"busca: {busca!r}" if busca
                  else "vozes em português")
        print("\n" + "=" * 70)
        print(f"  {titulo}")
        print("=" * 70)
        mostrar_pagina(vozes, pagina, total, em_uso)

        print("\n  número = ouvir | s <n> = salvar | n/p = página | "
              "b <termo> = buscar | q = sair")
        try:
            entrada = input("  → ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if not entrada or entrada.lower() == "q":
            return

        if entrada.lower() == "n":
            if pagina * POR_PAGINA < total:
                pagina += 1
            else:
                print("  já é a última página")
            continue

        if entrada.lower() == "p":
            pagina = max(1, pagina - 1)
            continue

        if entrada.lower().startswith("b "):
            busca, pagina, so_minhas = entrada[2:].strip(), 1, False
            continue

        if entrada.lower().startswith("s "):
            resto = entrada[2:].strip()
            if resto.isdigit() and 1 <= int(resto) <= len(vozes):
                v = vozes[int(resto) - 1]
                salvar_voz("voz_fish", v.id, v.title or "?")
                em_uso = v.id
            else:
                print("  número inválido")
            continue

        if entrada.isdigit() and 1 <= int(entrada) <= len(vozes):
            v = vozes[int(entrada) - 1]
            print(f"  {v.title}")
            ouvir_fish(sessao, v.id)
            print(f'  id: {v.id}    (digite "s {entrada}" para usar esta)')
        else:
            print("  não entendi")


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
    em_uso = ler_config().get("voz_edge", "pt-BR-AntonioNeural")

    print("\n  Vozes gratuitas do Edge TTS — usadas só se a Fish Audio falhar\n")
    for i, v in enumerate(vozes, 1):
        marca = "  <<< EM USO" if v["ShortName"] == em_uso else ""
        genero = "masculina" if v["Gender"] == "Male" else "feminina"
        print(f"  [{i:>2}] {v['ShortName']:<34} {genero:<10} "
              f"{v['Locale']}{marca}")

    while True:
        print("\n  número = ouvir | s <n> = salvar | q = sair")
        try:
            entrada = input("  → ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not entrada or entrada.lower() == "q":
            return

        salvar = entrada.lower().startswith("s ")
        numero = entrada[2:].strip() if salvar else entrada
        if not numero.isdigit() or not (1 <= int(numero) <= len(vozes)):
            print("  número inválido")
            continue

        v = vozes[int(numero) - 1]
        if salvar:
            salvar_voz("voz_edge", v["ShortName"], v["ShortName"])
            em_uso = v["ShortName"]
            continue

        print(f"  {v['ShortName']} — gerando...", end="", flush=True)
        try:
            mp3 = asyncio.run(gerar(v["ShortName"]))
            dec = miniaudio.decode(mp3,
                                   output_format=miniaudio.SampleFormat.SIGNED16,
                                   nchannels=1, sample_rate=24000)
            print(" tocando")
            tocar(np.array(dec.samples, dtype=np.int16), 24000)
        except Exception as e:
            print(f" falhou: {e}")


def main() -> None:
    args = sys.argv[1:]

    if "--edge" in args:
        modo_edge()
        return

    modo_fish(
        sort_by="created_at" if "--novas" in args else "task_count",
        so_minhas="--minhas" in args,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(0)
