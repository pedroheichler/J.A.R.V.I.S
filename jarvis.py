"""
JARVIS - Assistente de Desktop Pessoal
======================================

COMO FUNCIONA (arquitetura):
  1. Jarvis ouve você pelo microfone (ou lê do teclado)
  2. Manda o comando pro Claude (claude-haiku-4-5) junto com o histórico
     da conversa e a lista de ferramentas disponíveis
  3. Claude decide sozinho quais ferramentas chamar — e pode chamar mais
     de uma no mesmo pedido
  4. O tool_runner do SDK executa as funções e devolve os resultados
  5. Claude compõe a resposta final, que o Jarvis fala em voz alta

Por que ferramentas (tool use)? Porque o schema de cada função é gerado
e validado pelo SDK. Não existe "JSON inválido" pra tratar, e adicionar
uma capacidade nova é só escrever mais uma função com @beta_tool.
"""

# =============================================================================
# IMPORTS — cada biblioteca tem uma função específica
# =============================================================================
import os           # lê variáveis de ambiente (ANTHROPIC_API_KEY)
from dotenv import load_dotenv
load_dotenv()      # carrega o arquivo .env automaticamente
import json         # parse do JSON que o Claude retorna
import webbrowser   # abre URLs no navegador padrão
import subprocess   # abre programas do sistema (chrome, calc, etc.)
import sys          # exit() para encerrar o programa
import threading    # roda detecção de palma em paralelo com o loop principal
import time         # controla intervalo entre palmas
import queue        # fila thread-safe para comunicar palma → ação
from collections import deque  # buffer curto do áudio anterior à fala

import anthropic    # SDK oficial da Anthropic — acessa o Claude
# beta_tool: transforma uma função Python normal em ferramenta que o Claude
# pode chamar. O schema é gerado sozinho a partir da assinatura e do docstring.
from anthropic import beta_tool

# SpeechRecognition: captura áudio do microfone e converte em texto
# Internamente usa a Google Speech API (gratuita para uso básico)
import speech_recognition as sr

# sounddevice + scipy: captura áudio do microfone (alternativa ao pyaudio)
# pyaudio não tem wheel pré-compilado para Python 3.14, então usamos sounddevice
import sounddevice as sd
import numpy as np
import scipy.io.wavfile as wav
import io

# pyttsx3: fallback de voz offline
import pyttsx3

# Fish Audio: TTS com voz clonada
import fish_audio_sdk as fish

# edge-tts: fallback gratuito
import edge_tts
import asyncio

# miniaudio: decodifica o MP3 do Edge TTS para PCM, em Python puro.
# Sem ele seria preciso um player externo (ffmpeg, mpg123, Windows Media
# Player), que é o que amarrava o Jarvis ao Windows.
import miniaudio

# urllib.parse: codifica strings para URLs (ex: "inteligência artificial"
# vira "intelig%C3%AAncia+artificial" para funcionar numa URL)
from urllib.parse import quote_plus
from datetime import datetime
from zoneinfo import ZoneInfo
import urllib.request
from supabase import create_client


# =============================================================================
# CONFIGURAÇÃO INICIAL
# =============================================================================

# Lê a chave da Anthropic de variável de ambiente — NUNCA coloca no código!
# Para configurar: set ANTHROPIC_API_KEY=sk-ant-... (Windows)
#                  export ANTHROPIC_API_KEY=sk-ant-... (Linux/Mac)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    print("ERRO: variável de ambiente ANTHROPIC_API_KEY não encontrada.")
    print("Configure com: set ANTHROPIC_API_KEY=sua-chave-aqui")
    sys.exit(1)

# Cria o cliente da Anthropic uma vez só — reutilizado em todas as chamadas
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Fish Audio
FISH_AUDIO_API_KEY = os.environ.get("FISH_AUDIO_API_KEY")
fish_client = fish.Session(FISH_AUDIO_API_KEY) if FISH_AUDIO_API_KEY else None
FISH_VOICE_ID = "a5b93aeddcc948c19ea04f0afe9d178c"

# Voz do Edge TTS — fallback gratuito
EDGE_VOICE = "pt-BR-AntonioNeural"

# Supabase — integração com Kronos
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_USER_ID = os.environ.get("SUPABASE_USER_ID")
# Service key bypassa o RLS — necessário para leitura server-side
supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY) if SUPABASE_URL and SUPABASE_SERVICE_KEY else None

# Event que sinaliza quando Jarvis está falando — pausa detecção de palma
# threading.Event é thread-safe, ao contrário de uma variável bool simples
jarvis_falando = threading.Event()

# -----------------------------------------------------------------------------
# MEMÓRIA DE CONVERSA
# -----------------------------------------------------------------------------
# A API da Anthropic é SEM ESTADO: ela não guarda nada entre chamadas.
# Quem lembra do passado é o nosso código — mandando o histórico junto
# a cada requisição.
#
# Formato: [{"role": "user", ...}, {"role": "assistant", ...}, ...]
# sempre alternando e SEMPRE começando com "user" (a API exige isso).
#
# É isso que faz funcionar:
#   "Abre o Kronos no VSCode"  →  {"action": "open_folder", "folder": "kronos"}
#   "Agora pesquisa no Google" →  Claude sabe que "agora" se refere ao Kronos
historico: list[dict] = []

# Quantas trocas (pergunta + resposta) manter na memória.
# Cada troca são ~50 tokens, então 10 trocas ≈ 500 tokens por chamada —
# irrelevante no custo, mas suficiente pra manter o contexto de uma conversa.
MAX_TROCAS = 10

# Inicializa o motor de voz pyttsx3 como fallback
engine = pyttsx3.init()
engine.setProperty("rate", 150)

# Inicializa o reconhecedor de voz
recognizer = sr.Recognizer()
# energy_threshold: sensibilidade do microfone (100-4000, padrão=300)
# Quanto menor, mais sensível — capta sons mais baixos mas também ruído
recognizer.energy_threshold = 300
# dynamic_energy_threshold: ajusta automaticamente pro ambiente
recognizer.dynamic_energy_threshold = True


# =============================================================================
# MAPA DE APLICATIVOS (Windows)
# =============================================================================
# Dicionário que mapeia nomes comuns para o comando de abertura no Windows.
# subprocess.Popen() executa esses comandos como se fossem no terminal.
#
# "calc": calculadora do Windows (built-in, não precisa de caminho completo)
# "chrome": precisa do caminho completo porque não está no PATH por padrão
#
# PERSONALIZE: adicione seus próprios apps aqui!
APPS = {
    "chrome":      r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "calculadora": "calc",
    "calc":        "calc",
    "bloco de notas": "notepad",
    "notepad":     "notepad",
    "explorador":  "explorer",
    "explorador de arquivos": "explorer",
    "file explorer": "explorer",
    "word":        r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
    "excel":       r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
    "vscode":      r"C:\Users\%USERNAME%\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    "vs code":     r"C:\Users\%USERNAME%\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    "spotify":     r"C:\Users\%USERNAME%\AppData\Roaming\Spotify\Spotify.exe",
    "whatsapp":    r"C:\Users\%USERNAME%\AppData\Local\WhatsApp\WhatsApp.exe",
}

# Pastas de projetos — abre direto no VSCode
# Adicione seus projetos aqui: "nome que você vai falar": "caminho da pasta"
PASTAS = {
    "kronos":    r"C:\Programação\Trabalho-Pessoal\app Kronos",
    "app kronos": r"C:\Programação\Trabalho-Pessoal\app Kronos",
    "nexpeed":   r"C:\Programação\Nexpeed\Nexpeed",
    "jarvis":    r"C:\Programação\Trabalho-Pessoal\Jarvis",
}


# =============================================================================
# SYSTEM PROMPT DO CLAUDE
# =============================================================================
# Este texto instrui o Claude sobre como se comportar.
# É enviado em cada chamada à API como "system message".
#
# É uma f-string: as listas de apps e projetos são montadas a partir dos
# dicionários APPS e PASTAS acima. Assim, quando você cadastrar um app novo,
# o Claude fica sabendo automaticamente — sem precisar editar este texto.
SYSTEM_PROMPT = f"""
Você é J.A.R.V.I.S. (Just A Rather Very Intelligent System), o assistente de IA do Tony Stark.

PERSONALIDADE:
- Formal e educado, chama o usuário de "senhor" ou "senhora"
- Tom calmo, preciso e levemente britânico
- Ocasionalmente sarcástico e irônico, como no filme
- Inteligente e direto — nunca prolixo
- Exemplos de respostas no estilo Jarvis:
  "Claro, senhor. Abrindo o Chrome imediatamente."
  "Pesquisa iniciada, senhor. Embora eu questione a utilidade dessa busca."
  "À sua disposição, senhor."
  "Feito. Posso sugerir também que o senhor descanse um pouco?"

COMO AGIR:
Você tem ferramentas para controlar o computador do senhor. Chame a ferramenta
adequada quando o pedido exigir uma ação, e responda direto quando for apenas
uma pergunta ou conversa — conversar não precisa de ferramenta.

Quando um pedido exigir mais de uma ação, chame várias ferramentas na mesma
resposta. "Abre o Kronos e me diz minhas tarefas" são duas ferramentas.

Depois de usar as ferramentas, responda em uma ou duas frases curtas dizendo o
que foi feito. Sua resposta é falada em voz alta, então escreva como se fosse
falar: sem listas, sem markdown, sem emojis, sem URLs soletradas.

APLICATIVOS DISPONÍVEIS:
{", ".join(sorted(APPS))}

PROJETOS DISPONÍVEIS:
{", ".join(sorted(PASTAS))}

Se o senhor pedir algo fora dessas listas, diga que não está cadastrado em vez
de adivinhar um nome parecido.

CONTEXTO DA CONVERSA:
Você recebe as mensagens anteriores desta sessão. Quando o senhor disser "ele",
"isso", "lá", "de novo" ou "agora", olhe o histórico para descobrir a que ele
se refere. Se continuar ambíguo, pergunte.

Responda sempre em português brasileiro.
"""


# =============================================================================
# FUNÇÕES AUXILIARES
# =============================================================================

# Ganho aplicado à voz. O PCM da Fish Audio vem baixo demais; o clip evita
# que a amplificação estoure e distorça.
GANHO_VOZ = 2.5

FISH_SAMPLE_RATE = 44100   # taxa do PCM devolvido pela Fish Audio
EDGE_SAMPLE_RATE = 24000   # taxa nativa do Edge TTS


def _falar_fish(texto: str) -> None:
    """
    Fala usando a Fish Audio, tocando em STREAMING.

    Antes o código fazia b"".join(...) e só começava a tocar depois que o
    áudio inteiro chegava — a espera aparecia como atraso em toda resposta.
    Agora cada pedaço vai pra placa de som assim que chega da rede.

    latency="high" pede um buffer maior à placa. Custa alguns milissegundos
    no começo e evita que uma variação da rede corte o áudio no meio.
    """
    sobra = b""

    with sd.OutputStream(
        samplerate=FISH_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        latency="high",
    ) as saida:
        for pedaco in fish_client.tts(
            fish.TTSRequest(
                reference_id=FISH_VOICE_ID,
                text=texto,
                format="pcm",
            )
        ):
            dados = sobra + pedaco

            # Cada amostra int16 ocupa 2 bytes. Se o pedaço terminar no meio
            # de uma amostra, o byte solto espera o pedaço seguinte — senão
            # np.frombuffer estoura.
            corte = len(dados) - (len(dados) % 2)
            sobra = dados[corte:]
            if corte == 0:
                continue

            amostras = np.frombuffer(dados[:corte], dtype=np.int16)
            amostras = np.clip(
                amostras * GANHO_VOZ, -32768, 32767
            ).astype(np.int16)
            saida.write(amostras)


def _falar_edge(texto: str) -> None:
    """
    Fala usando o Edge TTS (fallback gratuito).

    O Edge TTS só entrega MP3, e o sounddevice só toca PCM. Antes isso era
    resolvido salvando um arquivo temporário e mandando o Windows Media
    Player tocar via PowerShell — o que prendia o Jarvis ao Windows.

    Agora o miniaudio decodifica o MP3 em memória e o sounddevice toca, igual
    ao caminho da Fish Audio. Sem arquivo temporário e sem programa externo.
    """
    async def baixar() -> bytes:
        conversa = edge_tts.Communicate(texto, EDGE_VOICE)
        partes = [
            evento["data"]
            async for evento in conversa.stream()
            if evento["type"] == "audio"
        ]
        return b"".join(partes)

    mp3 = asyncio.run(baixar())

    decodificado = miniaudio.decode(
        mp3,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=EDGE_SAMPLE_RATE,
    )
    amostras = np.array(decodificado.samples, dtype=np.int16)

    sd.play(amostras, samplerate=EDGE_SAMPLE_RATE)
    sd.wait()


def falar(texto: str) -> None:
    """
    Faz o Jarvis falar o texto em voz alta E imprime no terminal.

    Prioridade:
    1. Fish Audio — se FISH_AUDIO_API_KEY estiver configurada (em streaming)
    2. Edge TTS — fallback gratuito
    3. pyttsx3 — fallback offline

    Todos os três tocam pelo sounddevice, sem depender de nenhum programa
    externo — é o que permite rodar no Raspberry Pi sem alteração.
    """
    jarvis_falando.set()  # ativa — detecção de palma ignora enquanto True
    print(f"[Jarvis] {texto}")

    # O try/finally garante que a flag seja liberada mesmo se tudo falhar.
    # Se ela ficasse presa em True, a detecção de palma morreria em silêncio.
    try:
        if fish_client:
            try:
                _falar_fish(texto)
                return
            except Exception as e:
                print(f"[AVISO] Fish Audio falhou, usando voz local: {e}")

        try:
            _falar_edge(texto)
            return
        except Exception as e:
            print(f"[AVISO] Edge TTS falhou, usando voz local: {e}")

        # Fallback final: voz offline do sistema
        engine.say(texto)
        engine.runAndWait()
    finally:
        # Pequena folga pro alto-falante silenciar antes de reabrir o microfone
        time.sleep(0.5)
        jarvis_falando.clear()  # desativa
    jarvis_falando.clear()


def volume_rms(bloco) -> float:
    """
    Mede o volume de um pedaço de áudio (RMS = raiz da média dos quadrados).

    RMS é a medida de energia do som. Um número só, que serve tanto pra
    detectar fala quanto pra detectar palma.
    """
    return float(np.sqrt(np.mean(bloco.astype(np.float32) ** 2)))


class DetectorDeFala:
    """
    Decide quando o senhor COMEÇOU e quando PAROU de falar.

    Substitui a gravação de duração fixa. Antes eram sempre 5 segundos: você
    esperava 5 s mesmo dizendo só "oi", e era cortado no meio de uma frase
    longa. Agora a gravação dura o tempo que a fala durar.

    A lógica é uma máquina de estados alimentada pelo volume de cada pedaço:

        ESPERANDO ──fala detectada──> GRAVANDO ──silêncio longo──> FIM
            │                             │
            └──ninguém falou──> TIMEOUT   └──passou do teto──> FIM

    Fica separada da captura de áudio de propósito: assim dá pra testar a
    lógica com sequências de volume, sem precisar de microfone.
    """

    ESPERANDO = "esperando"
    GRAVANDO = "gravando"
    FIM = "fim"
    TIMEOUT = "timeout"

    def __init__(self, limiar: float, chunk_s: float,
                 silencio_final: float = 0.9, espera_max: float = 6.0,
                 fala_max: float = 20.0, min_fala: float = 0.25):
        self.limiar = limiar

        # Silêncio que encerra a fala. Curto demais corta você no meio de uma
        # pausa pra pensar; longo demais deixa a resposta lenta.
        self.CHUNKS_SILENCIO_FINAL = max(1, int(silencio_final / chunk_s))
        # Se ninguém falar nesse tempo, desiste e devolve TIMEOUT.
        self.CHUNKS_ESPERA_MAX = max(1, int(espera_max / chunk_s))
        # Teto de segurança: nunca grava mais que isso.
        self.CHUNKS_FALA_MAX = max(1, int(fala_max / chunk_s))
        # Fala curta demais é ruído (tosse, porta batendo), não comando.
        self.CHUNKS_MIN_FALA = max(1, int(min_fala / chunk_s))

        self.estado = self.ESPERANDO
        self.chunks_totais = 0
        self.chunks_gravando = 0
        self.chunks_silencio = 0
        self.chunks_com_voz = 0

    def processa(self, volume: float) -> str:
        """Consome um pedaço de áudio e devolve o estado atual."""
        self.chunks_totais += 1
        tem_voz = volume > self.limiar

        if self.estado == self.ESPERANDO:
            if tem_voz:
                self.estado = self.GRAVANDO
                self.chunks_gravando = 1
                self.chunks_com_voz = 1
                self.chunks_silencio = 0
            elif self.chunks_totais >= self.CHUNKS_ESPERA_MAX:
                self.estado = self.TIMEOUT

        elif self.estado == self.GRAVANDO:
            self.chunks_gravando += 1

            if tem_voz:
                self.chunks_com_voz += 1
                self.chunks_silencio = 0
            else:
                self.chunks_silencio += 1
                if self.chunks_silencio >= self.CHUNKS_SILENCIO_FINAL:
                    if self.chunks_com_voz >= self.CHUNKS_MIN_FALA:
                        self.estado = self.FIM
                    else:
                        # Foi um estalo, não uma frase. Volta a esperar.
                        self.estado = self.ESPERANDO
                        self.chunks_silencio = 0

            if self.chunks_gravando >= self.CHUNKS_FALA_MAX:
                self.estado = self.FIM

        return self.estado


def ouvir_microfone() -> str | None:
    """
    Captura áudio do microfone e converte em texto usando Google Speech API.

    Grava pelo tempo que a fala durar, em vez de uma janela fixa. O limiar é
    calibrado a cada chamada medindo o ruído de fundo por 300 ms — assim ele
    se adapta ao seu microfone e ao barulho da sala, sem número mágico.
    """
    SAMPLE_RATE = 16000       # Hz — qualidade suficiente para voz
    CHUNK = 0.03              # 30 ms por pedaço
    BLOCO = int(SAMPLE_RATE * CHUNK)

    CHUNKS_CALIBRACAO = 10    # 300 ms medindo o silêncio da sala
    FATOR_RUIDO = 3.5         # fala precisa ser 3,5x o ruído de fundo
    LIMIAR_MINIMO = 250       # piso, pra sala silenciosa não ficar sensível demais
    PRE_ROLL = 5              # 150 ms guardados antes da fala começar

    DEBUG = os.environ.get("JARVIS_DEBUG_FALA") == "1"

    jarvis_falando.set()  # pausa detecção de palma enquanto o senhor fala
    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=BLOCO,
        ) as stream:
            # ----- Calibração: quanto é "silêncio" nesta sala, agora? -----
            ruidos = []
            for _ in range(CHUNKS_CALIBRACAO):
                bloco, _ = stream.read(BLOCO)
                ruidos.append(volume_rms(bloco))
            # mediana em vez de média: um estalo isolado não estraga a medida
            ruido = float(np.median(ruidos))
            limiar = max(LIMIAR_MINIMO, ruido * FATOR_RUIDO)

            if DEBUG:
                print(f"[fala:debug] ruído de fundo={ruido:.0f} limiar={limiar:.0f}")

            detector = DetectorDeFala(limiar, CHUNK)
            print("\n[Jarvis] Ouvindo...")

            # PRE_ROLL guarda os últimos pedaços de "silêncio". Quando a fala
            # começa, eles entram na gravação — senão o primeiro som da
            # palavra é cortado e o reconhecimento erra.
            pre_roll = deque(maxlen=PRE_ROLL)
            pedacos = []
            comecou = False

            while True:
                bloco, _ = stream.read(BLOCO)
                estado = detector.processa(volume_rms(bloco))

                if estado == DetectorDeFala.ESPERANDO:
                    pre_roll.append(bloco.copy())
                elif estado in (DetectorDeFala.GRAVANDO, DetectorDeFala.FIM):
                    if not comecou:
                        pedacos.extend(pre_roll)
                        comecou = True
                    pedacos.append(bloco.copy())

                if estado == DetectorDeFala.FIM:
                    break
                if estado == DetectorDeFala.TIMEOUT:
                    return None

    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"[ERRO] Falha ao gravar áudio: {e}")
        return None
    finally:
        jarvis_falando.clear()  # reativa detecção de palma

    if not pedacos:
        return None

    audio_data = np.concatenate(pedacos)
    if DEBUG:
        print(f"[fala:debug] gravado {len(audio_data) / SAMPLE_RATE:.1f}s")

    # Salva o áudio gravado num arquivo WAV temporário em memória
    # SpeechRecognition precisa de um arquivo WAV para processar
    buffer = io.BytesIO()
    wav.write(buffer, SAMPLE_RATE, audio_data)
    buffer.seek(0)  # volta ao início do buffer para leitura

    try:
        with sr.AudioFile(buffer) as source:
            audio = recognizer.record(source)

        # language="pt-BR": reconhecimento em português do Brasil
        texto = recognizer.recognize_google(audio, language="pt-BR")
        print(f"[Você disse] {texto}")
        return texto.lower().strip()
    except sr.UnknownValueError:
        return None
    except sr.RequestError as e:
        print(f"[ERRO] Falha na API de voz: {e}")
        return None


# =============================================================================
# FERRAMENTAS DO JARVIS
# =============================================================================
# Cada função abaixo é uma ferramenta que o Claude pode chamar sozinho.
#
# O decorador @beta_tool lê a assinatura e o docstring da função e monta o
# schema JSON automaticamente:
#   - a primeira parte do docstring vira a DESCRIÇÃO (é por ela que o Claude
#     decide se aquela ferramenta serve pro pedido — por isso ela diz
#     explicitamente QUANDO usar)
#   - a seção "Args:" vira a descrição de cada parâmetro
#
# Toda ferramenta devolve uma STRING descrevendo o que aconteceu. Essa string
# volta pro Claude, que a usa pra compor a resposta falada. Quando algo dá
# errado, devolvemos o erro como texto em vez de levantar exceção — assim o
# Claude entende a falha e pode se explicar ou tentar outro caminho.


@beta_tool
def abrir_aplicativo(app: str) -> str:
    """Abre um aplicativo instalado no computador do senhor.

    Use quando ele pedir para abrir, iniciar ou executar um programa.
    Para sites use abrir_site; para pastas de projeto use abrir_pasta_de_projeto.

    Args:
        app: Nome do aplicativo em minúsculas, exatamente como aparece na
            lista de aplicativos disponíveis.
    """
    caminho = APPS.get(app.lower().strip())
    if not caminho:
        return f"O aplicativo '{app}' não está cadastrado na lista de aplicativos."

    try:
        # os.path.expandvars resolve %USERNAME% e outras variáveis do Windows
        subprocess.Popen([os.path.expandvars(caminho)])
        return f"Aplicativo '{app}' aberto."
    except FileNotFoundError:
        return f"'{app}' está cadastrado, mas o executável não existe em {caminho}."
    except Exception as e:
        return f"Falha ao abrir '{app}': {e}"


@beta_tool
def abrir_site(url: str) -> str:
    """Abre um endereço da web no navegador padrão.

    Use quando o senhor citar um site ou endereço específico.
    Para buscar um assunto na internet, use pesquisar_no_google.

    Args:
        url: Endereço do site, por exemplo github.com
    """
    url = url.strip()
    if not url:
        return "Nenhum endereço foi informado."

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    webbrowser.open(url)
    return f"Site {url} aberto no navegador."


@beta_tool
def pesquisar_no_youtube(busca: str) -> str:
    """Abre uma pesquisa no YouTube.

    Use para pedidos de vídeo ou música, ou quando ele mencionar o YouTube.

    Args:
        busca: O que pesquisar, por exemplo "lofi hip hop".
    """
    # quote_plus codifica a busca pra URL: "ia generativa" vira "ia+generativa"
    webbrowser.open(f"https://www.youtube.com/results?search_query={quote_plus(busca)}")
    return f"Pesquisa por '{busca}' aberta no YouTube."


@beta_tool
def pesquisar_no_google(busca: str) -> str:
    """Abre uma pesquisa no Google.

    Use quando o senhor pedir para pesquisar, buscar ou procurar algo.
    Se você já sabe a resposta, responda direto em vez de usar esta ferramenta.

    Args:
        busca: O que pesquisar.
    """
    webbrowser.open(f"https://www.google.com/search?q={quote_plus(busca)}")
    return f"Pesquisa por '{busca}' aberta no Google."


@beta_tool
def abrir_pasta_de_projeto(pasta: str) -> str:
    """Abre uma pasta de projeto no VS Code.

    Use quando o senhor citar um dos projetos da lista de projetos disponíveis.

    Args:
        pasta: Nome do projeto em minúsculas, como na lista de projetos
            disponíveis.
    """
    caminho = PASTAS.get(pasta.lower().strip())
    if not caminho:
        return f"O projeto '{pasta}' não está cadastrado na lista de projetos."

    try:
        subprocess.Popen(["code", caminho])
    except FileNotFoundError:
        # 'code' não está no PATH — tenta pelo caminho completo do VS Code
        vscode = os.path.expandvars(
            r"C:\Users\%USERNAME%\AppData\Local\Programs\Microsoft VS Code\Code.exe"
        )
        try:
            subprocess.Popen([vscode, caminho])
        except Exception as e:
            return f"Não consegui abrir o VS Code: {e}"

    return f"Projeto '{pasta}' aberto no VS Code."


@beta_tool
def hora_e_clima() -> str:
    """Informa a hora atual de Brasília e o clima em Goiânia.

    Use quando o senhor cumprimentar (bom dia, boa tarde, boa noite) ou
    perguntar as horas, a temperatura, o clima ou se vai chover.
    """
    return bom_dia_jarvis()


@beta_tool
def tarefas_de_hoje() -> str:
    """Consulta as tarefas pendentes de hoje no Kronos.

    Use quando o senhor perguntar sobre tarefas, agenda, compromissos
    ou o que ele precisa fazer hoje.
    """
    return buscar_tarefas_hoje()


# Lista entregue ao Claude a cada chamada. Para criar uma ferramenta nova,
# escreva a função com @beta_tool acima e acrescente o nome dela aqui — não
# é preciso mexer no system prompt nem em mais nada.
FERRAMENTAS = [
    abrir_aplicativo,
    abrir_site,
    pesquisar_no_youtube,
    pesquisar_no_google,
    abrir_pasta_de_projeto,
    hora_e_clima,
    tarefas_de_hoje,
]


# =============================================================================
# PROCESSAMENTO DO COMANDO
# =============================================================================

def processar_comando(texto_usuario: str) -> None:
    """
    Manda o comando pro Claude, executa as ferramentas que ele pedir e fala
    a resposta final.

    O tool_runner cuida do ciclo inteiro: chama a API, vê que o Claude quer
    usar ferramentas, executa as funções, devolve os resultados e repete até
    o Claude parar de pedir ferramentas. `until_done()` roda tudo isso e
    entrega só a mensagem final.

    max_iterations=8 é uma trava de segurança: mesmo que algo dê errado, o
    ciclo não roda pra sempre.
    """
    global historico

    # Histórico anterior + a pergunta nova. Só gravamos no `historico` depois
    # que der certo — senão uma falha de rede deixaria uma pergunta órfã.
    mensagens = historico + [{"role": "user", "content": texto_usuario}]

    try:
        runner = client.beta.messages.tool_runner(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            tools=FERRAMENTAS,
            messages=mensagens,
            max_iterations=8,
        )
        resposta = runner.until_done()
    except anthropic.APIError as e:
        print(f"[ERRO] Falha na API da Anthropic: {e}")
        falar("Não consegui completar o pedido, senhor.")
        return

    # A resposta final pode vir em vários blocos de texto — junta todos.
    # Blocos que não são de texto (tool_use, por exemplo) são ignorados aqui.
    texto_final = "".join(
        bloco.text for bloco in resposta.content if bloco.type == "text"
    ).strip()

    if not texto_final:
        texto_final = "Feito, senhor."

    falar(texto_final)

    # Guarda só o texto da conversa (pergunta e resposta falada). Os blocos
    # de tool_use/tool_result ficam de fora de propósito: manter o histórico
    # como pares user/assistant simples deixa o corte abaixo trivial e seguro.
    historico.append({"role": "user", "content": texto_usuario})
    historico.append({"role": "assistant", "content": texto_final})

    # Descarta as trocas mais antigas. Como sempre gravamos em pares, cortar
    # um número par pelo fim garante que o histórico continue começando com
    # "user" — que é o que a API exige.
    if len(historico) > MAX_TROCAS * 2:
        historico = historico[-(MAX_TROCAS * 2):]


def buscar_tarefas_hoje() -> str:
    """
    Busca tarefas de hoje no Supabase (Kronos) e retorna texto para o Jarvis falar.
    Filtra por date = hoje e status = pending.
    """
    if not supabase_client:
        return "Não consegui conectar ao Kronos, senhor. Verifique as credenciais do Supabase."

    try:
        from datetime import timezone, timedelta
        brasilia = timezone(timedelta(hours=-3))
        hoje = datetime.now(brasilia).strftime("%Y-%m-%d")

        resposta = (
            supabase_client.table("tasks")
            .select("title, priority, time, status")
            .eq("user_id", SUPABASE_USER_ID)
            .eq("date", hoje)
            .eq("status", "pending")
            .order("time")
            .execute()
        )

        tarefas = resposta.data

        if not tarefas:
            return "Nenhuma tarefa pendente para hoje, senhor. Agenda limpa."

        total = len(tarefas)
        alta = [t for t in tarefas if t.get("priority") == "high"]

        texto = f"Senhor, o senhor tem {total} tarefa{'s' if total > 1 else ''} pendente{'s' if total > 1 else ''} hoje. "

        if alta:
            texto += f"{len(alta)} de alta prioridade: "
            texto += ", ".join(t["title"] for t in alta[:3])
            texto += ". "

        outras = [t for t in tarefas if t.get("priority") != "high"][:3]
        if outras:
            texto += "Demais tarefas: "
            texto += ", ".join(t["title"] for t in outras)
            texto += "."

        return texto

    except Exception as e:
        print(f"[ERRO] Supabase: {e}")
        return "Não consegui acessar o Kronos no momento, senhor."


def bom_dia_jarvis() -> str:
    """
    Monta a saudação do Jarvis com hora real de Brasília e clima de Goiânia.
    Usa wttr.in — grátis, sem API key.
    """
    # Hora atual em Brasília
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    hora = agora.strftime("%H:%M")

    # Saudação baseada no horário
    h = agora.hour
    if 5 <= h < 12:
        saudacao = "Bom dia"
    elif 12 <= h < 18:
        saudacao = "Boa tarde"
    else:
        saudacao = "Boa noite"

    # Clima de Goiânia via wttr.in
    try:
        url = "https://wttr.in/Goiania?format=j1"
        with urllib.request.urlopen(url, timeout=5) as r:
            dados = json.loads(r.read())

        temp = dados["current_condition"][0]["temp_C"]
        sensacao = dados["current_condition"][0]["FeelsLikeC"]
        descricao_cod = int(dados["current_condition"][0]["weatherCode"])
        chuva_mm = dados["current_condition"][0]["precipMM"]

        # Traduz código de clima para português
        if descricao_cod in [113]:
            tempo = "céu limpo"
        elif descricao_cod in [116, 119, 122]:
            tempo = "nublado"
        elif descricao_cod in [176, 263, 266, 293, 296, 299, 302, 305, 308]:
            tempo = "com chuva"
        elif descricao_cod in [200, 386, 389]:
            tempo = "com trovoadas"
        else:
            tempo = "parcialmente nublado"

        vai_chover = float(chuva_mm) > 0.5
        previsao = "Há previsão de chuva, senhor." if vai_chover else "Sem previsão de chuva."

        clima = f"{temp} graus em Goiânia, {tempo}. {previsao}"
    except Exception:
        clima = "Não consegui obter o clima no momento, senhor."

    return f"{saudacao}, senhor. São {hora}, horário de Brasília. {clima}"


def eh_comando_saida(texto: str) -> bool:
    """
    Verifica se o usuário quer encerrar o programa.

    Compara a FRASE INTEIRA, não pedaço dela. Antes isso usava
    `palavra in texto`, o que fazia "fechar o chrome" e "desligar o monitor"
    encerrarem o Jarvis — porque "fechar" e "desligar" apareciam dentro deles.

    Por isso "fechar"/"desligar" sozinhos não estão na lista: são ambíguos.
    Só contam quando o usuário diz explicitamente que é o Jarvis.
    """
    FRASES_SAIDA = {
        "sair", "sai", "encerrar", "encerra", "exit", "quit",
        "tchau", "xau", "adeus", "falou", "até mais", "ate mais",
        "fechar jarvis", "fecha o jarvis", "fechar o jarvis",
        "desligar jarvis", "desliga o jarvis", "desligar o jarvis",
        "encerrar jarvis", "encerra o jarvis",
    }

    # Tira espaços das pontas e pontuação (ex: "tchau!" vira "tchau")
    texto_limpo = texto.lower().strip().strip(".,!?;:")
    return texto_limpo in FRASES_SAIDA


# =============================================================================
# PALAVRA DE ATIVAÇÃO (wake word)
# =============================================================================
# Sem isso o Jarvis reage a tudo que ouve. Com isso ele só age quando é
# chamado pelo nome — como a Alexa.
#
# "Jarvis" é um nome inglês, e o reconhecimento em português às vezes escreve
# diferente. Se o seu sotaque ou microfone produzir outra grafia, acrescente
# aqui: o terminal sempre imprime "[Você disse] ..." com o que foi entendido,
# então dá pra ver exatamente o que precisa entrar na lista.
PALAVRAS_ATIVACAO = (
    "jarvis", "járvis", "jarvez", "jarves", "jarvis'", "jávis", "travis",
)


def extrair_comando_apos_ativacao(texto: str) -> str | None:
    """
    Separa a palavra de ativação do comando que vem depois dela.

    Três resultados possíveis:
      "jarvis abre o chrome"  -> "abre o chrome"  (chamou e já mandou)
      "jarvis"                -> ""               (só chamou, comando vem depois)
      "abre o chrome"         -> None             (não falou comigo, ignora)

    A diferença entre "" e None importa: string vazia significa que o senhor
    chamou e o Jarvis deve perguntar o que quer; None significa que a frase
    não era dirigida a ele e deve ser descartada em silêncio.
    """
    t = texto.lower().strip()

    for palavra in PALAVRAS_ATIVACAO:
        posicao = t.find(palavra)
        if posicao != -1:
            resto = t[posicao + len(palavra):]
            # Tira a vírgula e o espaço de "Jarvis, abre o chrome"
            return resto.strip(" ,.!?;:")

    return None


# =============================================================================
# DETECÇÃO DE PALMA
# =============================================================================

# Músicas do AC/DC para tocar aleatoriamente a cada palma
ACDC_MUSICAS = [
    ("Back in Black",    "https://www.youtube.com/watch?v=pAgnJDJN4VA"),
    ("Thunderstruck",    "https://www.youtube.com/watch?v=v2AC41dglnM"),
    ("Highway to Hell",  "https://www.youtube.com/watch?v=l482T0yNkeo"),
    ("Shoot to Thrill",  "https://www.youtube.com/watch?v=Co_G0TgSbhI"),
    ("Hells Bells",      "https://www.youtube.com/watch?v=etAIpkdhU9Q"),
]

class DetectorDePalma:
    """
    Distingue palma de fala pelo FORMATO do som, não só pelo volume.

    Uma palma é um transiente: sai do silêncio, estoura e some — tudo em
    menos de 100 ms. Fala é sustentada: o volume fica alto por vários
    décimos de segundo seguidos, e cai só nas pausas entre as palavras.

    A versão antiga olhava apenas "o volume passou do limiar?". Como o
    intervalo entre duas palavras faladas (0,1 s a 0,8 s) é exatamente a
    janela usada para "duas palmas", qualquer frase disparava a música.

    Por isso um estouro só conta como palma quando cumpre as DUAS condições:
      1. vem depois de um silêncio (a fala emenda uma sílaba na outra)
      2. termina rápido (a fala se sustenta por muito mais tempo)

    A classe é separada da captura de áudio de propósito: assim dá para
    testar a lógica com sequências de volume sem precisar de microfone.
    """

    def __init__(self, limiar: float, chunk_s: float,
                 janela: float, cooldown: float):
        self.LIMIAR = limiar
        # Histerese: para SAIR do estouro exigimos um volume bem menor do que
        # para ENTRAR. Sem isso o volume fica oscilando em torno do limiar.
        self.LIMIAR_BAIXO = limiar * 0.4
        self.JANELA = janela
        self.COOLDOWN = cooldown

        # Um estouro de até ~120 ms é transiente (palma). Mais que isso é fala.
        self.MAX_CHUNKS_BURST = max(1, int(0.12 / chunk_s))
        # Exige ~150 ms de silêncio antes do estouro.
        self.MIN_CHUNKS_SILENCIO = max(1, int(0.15 / chunk_s))

        self.chunks_silencio = self.MIN_CHUNKS_SILENCIO
        self.em_burst = False
        self.chunks_burst = 0
        self.inicio_burst = 0.0
        # -inf = "nunca aconteceu". Zerar aqui faria o cooldown bloquear os
        # primeiros segundos sempre que a base de tempo começasse perto de zero.
        self.ultima_palma = float("-inf")
        self.ultimo_disparo = float("-inf")

    def processa(self, volume: float, agora: float) -> bool:
        """Consome um pedaço de áudio. Devolve True no par de palmas."""
        if self.em_burst:
            if volume > self.LIMIAR_BAIXO:
                self.chunks_burst += 1
                if self.chunks_burst > self.MAX_CHUNKS_BURST:
                    # Comprido demais: é fala ou ruído contínuo. Descarta.
                    self.em_burst = False
                    self.chunks_silencio = 0
                return False

            # Acabou dentro do tempo — transiente confirmado
            self.em_burst = False
            self.chunks_silencio = 0
            return self._registrar(self.inicio_burst)

        if volume > self.LIMIAR:
            if self.chunks_silencio >= self.MIN_CHUNKS_SILENCIO:
                self.em_burst = True
                self.chunks_burst = 1
                self.inicio_burst = agora
            else:
                # Estouro sem silêncio antes: fala emendada. Ignora.
                self.chunks_silencio = 0
            return False

        if volume < self.LIMIAR_BAIXO:
            self.chunks_silencio += 1
        else:
            self.chunks_silencio = 0
        return False

    def _registrar(self, quando: float) -> bool:
        """Guarda a palma e avisa quando fecharam duas dentro da janela."""
        if quando - self.ultimo_disparo < self.COOLDOWN:
            return False

        intervalo = quando - self.ultima_palma
        if 0.08 < intervalo < self.JANELA:
            self.ultima_palma = float("-inf")   # par consumido, recomeça
            self.ultimo_disparo = quando
            return True

        self.ultima_palma = quando
        return False


def detectar_palma():
    """
    Roda em background e monitora o microfone continuamente.
    O callback APENAS detecta e envia para uma fila — nunca executa ações.
    Uma thread separada consome a fila e executa falar() + webbrowser.
    Isso evita conflitos com o sounddevice.

    Para calibrar o limiar no seu microfone, rode com JARVIS_DEBUG_PALMA=1:
    o volume de cada pedaço é impresso, então dá pra ver quanto marca uma
    palma e quanto marca a sua voz.
    """
    CHUNK = 0.01       # 10 ms — resolução fina o bastante pra medir o estouro
    SAMPLE_RATE = 16000
    LIMIAR = 2500      # aumente se disparar com barulho de fundo
    JANELA = 0.8       # tempo máximo entre as duas palmas (segundos)
    COOLDOWN = 4.0     # segundos de bloqueio após tocar música

    DEBUG = os.environ.get("JARVIS_DEBUG_PALMA") == "1"

    fila = queue.Queue()
    detector = DetectorDePalma(LIMIAR, CHUNK, JANELA, COOLDOWN)

    def callback(indata, frames, time_info, status):
        if jarvis_falando.is_set():
            return

        volume = volume_rms(indata)

        if DEBUG and volume > LIMIAR * 0.4:
            print(f"[palma:debug] volume={volume:7.0f} limiar={LIMIAR}")

        if detector.processa(volume, time.time()):
            fila.put("palma")   # envia sinal para a thread executora

    def executor():
        musica_index = 0
        while True:
            try:
                fila.get(timeout=1)
                nome, url = ACDC_MUSICAS[musica_index % len(ACDC_MUSICAS)]
                musica_index += 1
                print(f"\n[Palma detectada] Tocando: {nome}")
                falar(f"Tocando {nome}, senhor.")
                time.sleep(2.5)
                webbrowser.open(url)
            except queue.Empty:
                continue

    threading.Thread(target=executor, daemon=True).start()

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=int(SAMPLE_RATE * CHUNK),
            callback=callback
        ):
            while True:
                time.sleep(0.1)
    except Exception as e:
        print(f"[AVISO] Detecção de palma desativada: {e}")


# =============================================================================
# LOOP PRINCIPAL
# =============================================================================

def main():
    """
    Loop principal do Jarvis.

    Lógica:
    1. Tenta capturar voz do microfone
    2. Se não ouvir nada, pede input pelo teclado
    3. Verifica se é comando de saída
    4. Manda pro Claude, que chama as ferramentas necessárias
    5. Volta pro passo 1
    """
    # Inicia detecção de palma em thread separada (daemon=True: encerra junto com o programa)
    thread_palma = threading.Thread(target=detectar_palma, daemon=True)
    thread_palma.start()

    # Com wake word, o Jarvis fica escutando mas só age quando é chamado
    # pelo nome. Para voltar ao comportamento antigo — reagir a tudo que
    # ouve, com o teclado como alternativa — rode com JARVIS_SEM_WAKE_WORD=1.
    usar_wake_word = os.environ.get("JARVIS_SEM_WAKE_WORD") != "1"

    # Vira False no primeiro erro de microfone e o Jarvis passa pro teclado.
    microfone_ok = True

    if usar_wake_word:
        falar("Olá, senhor. Diga o meu nome quando precisar de mim.")
    else:
        falar("Olá, senhor. Como posso ajudá-lo?")

    while True:
        # ----- Captura de entrada -----
        comando = None

        if microfone_ok:
            try:
                ouvido = ouvir_microfone()
            except Exception as e:
                # Microfone sumiu (desconectado, em uso por outro programa).
                # Passa pro teclado e não tenta mais o microfone.
                print(f"[AVISO] Microfone indisponível: {e}")
                microfone_ok = False
                ouvido = None

            if microfone_ok:
                if not ouvido:
                    continue  # ninguém falou — volta a escutar

                if usar_wake_word:
                    comando = extrair_comando_apos_ativacao(ouvido)

                    if comando is None:
                        continue  # falaram, mas não com o Jarvis

                    if not comando:
                        # Chamou o nome sem mandar comando. Pergunta e escuta.
                        falar("Pois não, senhor?")
                        comando = ouvir_microfone()
                        if not comando:
                            continue
                else:
                    comando = ouvido

        # Teclado — só quando o microfone não está disponível
        if not comando and not microfone_ok:
            try:
                comando = input("\n[Digite seu comando] → ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                # Ctrl+C ou fim de stream — encerra graciosamente
                falar("Até mais!")
                break

        # Ignora entrada vazia
        if not comando:
            continue

        # ----- Verifica saída -----
        if eh_comando_saida(comando):
            falar("Até mais!")
            break

        # ----- Processa com Claude e executa as ferramentas -----
        # processar_comando faz tudo: chama a API, executa as ferramentas
        # que o Claude pedir e fala a resposta final.
        print(f"[Processando] '{comando}'")
        processar_comando(comando)


# =============================================================================
# ENTRY POINT
# =============================================================================
# if __name__ == "__main__": garante que main() só roda quando executamos
# este arquivo diretamente (python jarvis.py), não quando importamos como módulo
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Jarvis] Encerrando...")
        falar("Até mais!")
        sys.exit(0)
