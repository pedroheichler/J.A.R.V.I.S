"""
CALIBRADOR DA PALAVRA DE ATIVAÇÃO
=================================

Descobre o que o reconhecimento de voz REALMENTE entende quando você fala o
nome do assistente — em vez de ficar adivinhando a grafia.

Como usar:
    python calibrar.py

Ele pede pra você falar o nome algumas vezes, mostra exatamente o que o
Google devolveu em cada tentativa e diz quais valores colocar no config.json.

Não altera nada sozinho: só mede e sugere.
"""
import io
import sys

import numpy as np
import scipy.io.wavfile as wav
import sounddevice as sd
import speech_recognition as sr

TENTATIVAS = 5
SAMPLE_RATE = 16000
CHUNK = 0.03
BLOCO = int(SAMPLE_RATE * CHUNK)


def volume(bloco) -> float:
    return float(np.sqrt(np.mean(bloco.astype(np.float32) ** 2)))


def gravar_uma_fala(reconhecedor) -> str | None:
    """Grava até o silêncio e devolve o texto reconhecido."""
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                        dtype="int16", blocksize=BLOCO) as stream:
        # Calibra o ruído da sala
        ruidos = [volume(stream.read(BLOCO)[0]) for _ in range(10)]
        limiar = max(250, float(np.median(ruidos)) * 3.5)

        print("   fale agora...", end="", flush=True)

        pedacos, falando, silencio, total = [], False, 0, 0
        while True:
            bloco, _ = stream.read(BLOCO)
            total += 1
            alto = volume(bloco) > limiar

            if alto:
                falando, silencio = True, 0
            elif falando:
                silencio += 1

            if falando:
                pedacos.append(bloco.copy())

            if falando and silencio > int(0.9 / CHUNK):
                break
            if total > int(8 / CHUNK):          # desiste depois de 8 s
                print(" (não ouvi nada)")
                return None

    print(f" ok ({len(pedacos) * CHUNK:.1f}s)")

    buffer = io.BytesIO()
    wav.write(buffer, SAMPLE_RATE, np.concatenate(pedacos))
    buffer.seek(0)

    try:
        with sr.AudioFile(buffer) as fonte:
            audio = reconhecedor.record(fonte)
        return reconhecedor.recognize_google(audio, language="pt-BR")
    except sr.UnknownValueError:
        return None
    except sr.RequestError as e:
        print(f"   [ERRO] API de voz indisponível: {e}")
        return None


def main() -> None:
    nome = input("Que nome você QUER usar? (ex: alfred) → ").strip().lower()
    if not nome:
        print("Nome vazio. Saindo.")
        return

    print(f"\nVou pedir pra você falar {TENTATIVAS} vezes.")
    print("Fale naturalmente, como falaria com o assistente.")
    print(f'Ex: "{nome}, que horas são?" ou só "{nome}".\n')

    reconhecedor = sr.Recognizer()
    resultados = []

    for i in range(1, TENTATIVAS + 1):
        input(f"[{i}/{TENTATIVAS}] Aperte Enter e fale... ")
        texto = gravar_uma_fala(reconhecedor)
        if texto:
            print(f"   >>> o Google entendeu: {texto!r}")
            resultados.append(texto.lower())
        else:
            print("   >>> não entendeu nada")

    print("\n" + "=" * 62)
    print("RESULTADO")
    print("=" * 62)

    if not resultados:
        print("\nNenhuma tentativa foi reconhecida. Isso NÃO é problema do")
        print("nome escolhido — é o microfone ou a conexão. Verifique:")
        print("  - o microfone certo está selecionado no Windows?")
        print("  - você está falando perto o suficiente?")
        print("  - a internet está funcionando? (o reconhecimento é online)")
        return

    # Junta todas as palavras ouvidas e ordena pelas mais frequentes
    from collections import Counter
    from difflib import SequenceMatcher

    palavras = Counter()
    for frase in resultados:
        for p in frase.split():
            palavras[p.strip(" ,.!?;:")] += 1

    print(f"\nO Google entendeu, nas {len(resultados)} tentativas:")
    for frase in resultados:
        print(f"   {frase!r}")

    # Quais palavras se parecem com o nome desejado?
    candidatas = []
    for palavra, vezes in palavras.items():
        nota = SequenceMatcher(None, palavra, nome).ratio()
        candidatas.append((nota, vezes, palavra))
    candidatas.sort(reverse=True)

    print(f"\nPalavras mais parecidas com {nome!r}:")
    for nota, vezes, palavra in candidatas[:6]:
        print(f"   {palavra!r:<20} semelhança {nota:>5.0%}   ouvida {vezes}x")

    melhor = candidatas[0] if candidatas else None
    print("\n" + "-" * 62)

    if melhor and melhor[0] >= 0.72:
        print(f"BOA NOTÍCIA: {melhor[2]!r} já casaria com o limiar padrão (72%).")
        print("Se não está funcionando, provavelmente o programa não foi")
        print("reiniciado depois de editar o config.json.")
    elif melhor and melhor[0] >= 0.5:
        sugerido = max(0.5, melhor[0] - 0.05)
        print(f"O reconhecimento devolve {melhor[2]!r}, que fica em "
              f"{melhor[0]:.0%} — abaixo do limiar padrão de 72%.")
        print("\nColoque isto no config.json:")
        print(f'   "palavras_ativacao": ["{nome}", "{melhor[2]}"],')
        print(f'   "limiar_ativacao": {sugerido:.2f}')
    else:
        print(f"O reconhecimento não chega perto de {nome!r} — o mais próximo")
        print(f"foi {melhor[2]!r} com apenas {melhor[0]:.0%}.")
        print("\nEsse nome não funciona bem em português. Use direto o que")
        print("ele entende:")
        mais_comum = palavras.most_common(1)[0][0]
        print(f'   "palavras_ativacao": ["{mais_comum}"],')
        print("\nOu escolha um nome que o reconhecimento acerte — palavras")
        print("do português funcionam muito melhor que nomes estrangeiros.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(0)
