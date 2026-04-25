"""
JARVIS - Assistente de Desktop Pessoal
======================================

COMO FUNCIONA (arquitetura):
  1. Jarvis ouve você pelo microfone (ou lê do teclado)
  2. Manda o comando pro Claude (claude-haiku-4-5)
  3. Claude devolve um JSON dizendo qual "action" executar
  4. Jarvis executa a action (abrir app, pesquisar, falar, etc.)

Por que JSON? Porque é estruturado. "Abrir chrome" pode virar
{"action": "open_app", "app": "chrome"} — fácil de processar no código.
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

import anthropic    # SDK oficial da Anthropic — acessa o Claude

# SpeechRecognition: captura áudio do microfone e converte em texto
# Internamente usa a Google Speech API (gratuita para uso básico)
import speech_recognition as sr

# sounddevice + scipy: captura áudio do microfone (alternativa ao pyaudio)
# pyaudio não tem wheel pré-compilado para Python 3.14, então usamos sounddevice
import sounddevice as sd
import numpy as np
import scipy.io.wavfile as wav
import tempfile
import io

# pyttsx3: fallback de voz offline
import pyttsx3

# Fish Audio: TTS com voz clonada
import fish_audio_sdk as fish

# edge-tts: fallback gratuito
import edge_tts
import asyncio

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
# A instrução mais importante: "retorne APENAS JSON válido"
# Sem esse aviso, o Claude pode responder em linguagem natural
# e o json.loads() vai falhar.
SYSTEM_PROMPT = """
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

INSTRUÇÃO TÉCNICA:
Analise o comando e retorne APENAS um JSON válido, sem texto adicional, markdown ou explicações.

O JSON deve ter obrigatoriamente o campo "action" com um dos valores abaixo:

1. open_app — para abrir um aplicativo
   Exemplo: {"action": "open_app", "app": "chrome"}

2. open_url — para abrir um site específico
   Exemplo: {"action": "open_url", "url": "https://github.com"}

3. youtube_search — para pesquisar no YouTube
   Exemplo: {"action": "youtube_search", "query": "lofi hip hop"}

4. google_search — para pesquisar no Google
   Exemplo: {"action": "google_search", "query": "previsão do tempo hoje"}

5. speak — para responder perguntas ou dar informações
   Exemplo: {"action": "speak", "text": "Claro, senhor. São Paulo é a maior cidade do Brasil."}

6. bom_dia — quando o usuário disser "bom dia", "boa tarde" ou "boa noite"
   Exemplo: {"action": "bom_dia"}

7. open_folder — para abrir uma pasta de projeto no VSCode
   Exemplo: {"action": "open_folder", "folder": "kronos"}

8. tarefas_hoje — quando o usuário perguntar sobre tarefas, agenda ou o que fazer hoje
   Exemplo: {"action": "tarefas_hoje"}

Regras:
- APENAS JSON, sem texto antes ou depois
- No campo "text" do speak, use frases no estilo Jarvis:
  "Imediatamente, senhor.", "Claro, senhor.", "Feito, senhor.",
  "À sua disposição, senhor.", "Posso sugerir...", "Sistemas operacionais, senhor."
- Para perguntas gerais, use "speak"
- Para "bom dia", "boa tarde" ou "boa noite", use "bom_dia"
- Para "pesquisa X no YouTube" ou "abre X no YouTube", use youtube_search
- Para "pesquisa X", "busca X" ou "procura X", use google_search
- Nomes de apps no campo "app" devem ser em minúsculas
- Responda sempre em português brasileiro
"""


# =============================================================================
# FUNÇÕES AUXILIARES
# =============================================================================

def falar(texto: str) -> None:
    """
    Faz o Jarvis falar o texto em voz alta E imprime no terminal.

    Prioridade:
    1. Fish Audio — se FISH_AUDIO_API_KEY estiver configurada
    2. Edge TTS — fallback gratuito
    3. pyttsx3 — fallback offline

    PCM = áudio cru sem compressão, toca direto pelo sounddevice sem precisar
    de ffmpeg ou qualquer decodificador externo.
    """
    jarvis_falando.set()  # ativa — detecção de palma ignora enquanto True
    print(f"[Jarvis] {texto}")

    if fish_client:
        try:
            audio_bytes = b"".join(
                fish_client.tts(
                    fish.TTSRequest(
                        reference_id=FISH_VOICE_ID,
                        text=texto,
                        format="pcm",
                    )
                )
            )
            audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
            # Amplifica o volume (2.0 = dobro) — clip evita distorção
            audio_array = np.clip(audio_array * 2.5, -32768, 32767).astype(np.int16)
            sd.play(audio_array, samplerate=44100)
            sd.wait()
            time.sleep(0.5)
            jarvis_falando.clear()  # desativa
            return
        except Exception as e:
            print(f"[AVISO] Fish Audio falhou, usando voz local: {e}")

    # Fallback: Edge TTS
    try:
        async def _falar_edge():
            communicate = edge_tts.Communicate(texto, EDGE_VOICE)
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp_path = f.name
            await communicate.save(tmp_path)
            subprocess.run(
                ["powershell", "-c",
                 f"$wmp = New-Object -ComObject WMPlayer.OCX; "
                 f"$wmp.URL = '{tmp_path}'; "
                 f"$wmp.controls.play(); "
                 f"Start-Sleep -Seconds ($wmp.currentMedia.duration + 1); "
                 f"$wmp.close()"],
                capture_output=True
            )
            os.unlink(tmp_path)
        asyncio.run(_falar_edge())
        time.sleep(0.5)
        jarvis_falando.clear()
        return
    except Exception as e:
        print(f"[AVISO] Edge TTS falhou, usando voz local: {e}")

    # Fallback final: pyttsx3
    engine.say(texto)
    engine.runAndWait()
    time.sleep(0.5)
    jarvis_falando.clear()


def ouvir_microfone() -> str | None:
    """
    Captura áudio do microfone e converte em texto usando Google Speech API.

    Usa sounddevice para gravar e SpeechRecognition para reconhecer.
    sounddevice grava por um tempo fixo (5s) — não detecta silêncio automaticamente,
    mas é compatível com Python 3.14 onde pyaudio não tem wheel disponível.
    """
    SAMPLE_RATE = 16000  # Hz — qualidade suficiente para voz
    DURACAO = 5          # segundos de gravação

    print("\n[Jarvis] Ouvindo... (você tem 5 segundos para falar)")

    jarvis_falando.set()  # pausa detecção de palma enquanto usuário fala
    try:
        audio_data = sd.rec(
            int(DURACAO * SAMPLE_RATE),
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16"
        )
        sd.wait()
    except KeyboardInterrupt:
        sd.stop()
        jarvis_falando.clear()
        raise
    except Exception as e:
        print(f"[ERRO] Falha ao gravar áudio: {e}")
        jarvis_falando.clear()
        return None
    jarvis_falando.clear()  # reativa detecção após gravar

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


def obter_comando_claude(texto_usuario: str) -> dict | None:
    """
    Manda o comando do usuário pro Claude e recebe o JSON de ação.

    Usa claude-haiku-4-5 porque:
    - É o modelo mais rápido e barato da Anthropic
    - Para classificar comandos simples, não precisa de um modelo grande
    - Latência menor = resposta mais ágil

    max_tokens=200: JSON de ação é pequeno, 200 tokens é mais que suficiente
    """
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": texto_usuario}
            ]
        )

        # response.content é uma lista de blocos — pegamos o texto do primeiro
        resposta_texto = response.content[0].text.strip()

        # Remove blocos de código markdown se o Claude os incluir (ex: ```json ... ```)
        if resposta_texto.startswith("```"):
            linhas = resposta_texto.split("\n")
            # Remove primeira linha (```json) e última (```)
            resposta_texto = "\n".join(linhas[1:-1])

        # json.loads() converte a string JSON em dicionário Python
        return json.loads(resposta_texto)

    except json.JSONDecodeError:
        # Claude retornou algo que não é JSON válido
        print(f"[ERRO] JSON inválido recebido: {resposta_texto}")
        return None
    except anthropic.APIError as e:
        print(f"[ERRO] Falha na API da Anthropic: {e}")
        return None


def executar_acao(acao: dict) -> None:
    """
    Recebe o dicionário de ação do Claude e executa o comando correspondente.

    Este é o "executor" do Jarvis. Claude decide O QUE fazer,
    esta função decide COMO fazer.
    """
    action = acao.get("action", "").lower()

    # -----------------------------------------------------------------
    # ACTION: open_app — abre um aplicativo instalado
    # -----------------------------------------------------------------
    if action == "open_app":
        app_nome = acao.get("app", "").lower()
        app_path = APPS.get(app_nome)

        if app_path:
            try:
                # os.path.expandvars: resolve %USERNAME% e outras variáveis
                app_path = os.path.expandvars(app_path)
                # subprocess.Popen: abre o processo sem bloquear o Jarvis
                subprocess.Popen([app_path])
                falar(f"Abrindo {app_nome}.")
            except FileNotFoundError:
                falar(f"Aplicativo não encontrado: {app_nome}")
            except Exception as e:
                falar("Não consegui abrir o aplicativo.")
                print(f"[ERRO] {e}")
        else:
            # App não está no nosso dicionário APPS
            falar(f"Aplicativo não encontrado: {app_nome}")

    # -----------------------------------------------------------------
    # ACTION: open_url — abre uma URL no navegador padrão
    # -----------------------------------------------------------------
    elif action == "open_url":
        url = acao.get("url", "")
        if url:
            # Garante que a URL tem protocolo (https://)
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            webbrowser.open(url)
            falar(f"Abrindo o site.")
        else:
            falar("URL não especificada.")

    # -----------------------------------------------------------------
    # ACTION: youtube_search — pesquisa no YouTube
    # -----------------------------------------------------------------
    elif action == "youtube_search":
        query = acao.get("query", "")
        if query:
            # quote_plus: codifica a query para URL
            # ex: "inteligência artificial" → "intelig%C3%AAncia+artificial"
            url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
            webbrowser.open(url)
            falar(f"Pesquisando {query} no YouTube.")
        else:
            falar("O que você quer pesquisar no YouTube?")

    # -----------------------------------------------------------------
    # ACTION: google_search — pesquisa no Google
    # -----------------------------------------------------------------
    elif action == "google_search":
        query = acao.get("query", "")
        if query:
            url = f"https://www.google.com/search?q={quote_plus(query)}"
            webbrowser.open(url)
            falar(f"Pesquisando {query} no Google.")
        else:
            falar("O que você quer pesquisar?")

    # -----------------------------------------------------------------
    # ACTION: speak — responde em voz e texto
    # -----------------------------------------------------------------
    elif action == "speak":
        texto = acao.get("text", "Não tenho resposta para isso.")
        falar(texto)

    # -----------------------------------------------------------------
    # ACTION: bom_dia — saudação com hora e clima real
    # -----------------------------------------------------------------
    # ACTION: open_folder — abre pasta no VSCode
    # -----------------------------------------------------------------
    elif action == "open_folder":
        folder_nome = acao.get("folder", "").lower()
        folder_path = PASTAS.get(folder_nome)

        if folder_path:
            try:
                subprocess.Popen(["code", folder_path])
                falar(f"Abrindo {folder_nome} no VSCode, senhor.")
            except FileNotFoundError:
                # 'code' não está no PATH — tenta pelo caminho completo
                vscode = os.path.expandvars(
                    r"C:\Users\%USERNAME%\AppData\Local\Programs\Microsoft VS Code\Code.exe"
                )
                subprocess.Popen([vscode, folder_path])
                falar(f"Abrindo {folder_nome} no VSCode, senhor.")
        else:
            falar(f"Pasta {folder_nome} não encontrada, senhor.")

    # -----------------------------------------------------------------
    elif action == "bom_dia":
        falar(bom_dia_jarvis())

    # -----------------------------------------------------------------
    # ACTION: tarefas_hoje — busca tarefas do Kronos
    # -----------------------------------------------------------------
    elif action == "tarefas_hoje":
        falar(buscar_tarefas_hoje())

    # -----------------------------------------------------------------
    # ACTION desconhecida
    # -----------------------------------------------------------------
    else:
        falar("Não sei como executar essa ação.")
        print(f"[AVISO] Action desconhecida: {action}")


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
    Comparação simples por palavras-chave — não precisa do Claude para isso.
    """
    palavras_saida = {"sair", "encerrar", "tchau", "fechar", "desligar", "exit", "quit"}
    # any(): retorna True se qualquer palavra estiver no texto
    return any(palavra in texto.lower() for palavra in palavras_saida)


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

def detectar_palma():
    """
    Roda em background e monitora o microfone continuamente.
    O callback APENAS detecta e envia para uma fila — nunca executa ações.
    Uma thread separada consome a fila e executa falar() + webbrowser.
    Isso evita conflitos com o sounddevice.
    """
    CHUNK = 0.05
    SAMPLE_RATE = 16000
    LIMIAR = 2500      # aumente se disparar com barulho de fundo
    JANELA = 0.8       # tempo máximo entre as duas palmas (segundos)
    COOLDOWN = 4.0     # segundos de bloqueio após tocar música

    fila = queue.Queue()
    ultima_palma = [0]
    ultimo_disparo = [0]

    def callback(indata, frames, time_info, status):
        if jarvis_falando.is_set():
            return

        volume = np.sqrt(np.mean(indata.astype(np.float32) ** 2))

        if volume > LIMIAR:
            agora = time.time()

            # Ignora se ainda está no cooldown pós-música
            if agora - ultimo_disparo[0] < COOLDOWN:
                return

            intervalo = agora - ultima_palma[0]
            if 0.1 < intervalo < JANELA:
                fila.put("palma")       # envia sinal para a thread executora
                ultima_palma[0] = 0
                ultimo_disparo[0] = agora
            else:
                ultima_palma[0] = agora

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
    4. Manda pro Claude → recebe JSON → executa
    5. Volta pro passo 1
    """
    # Inicia detecção de palma em thread separada (daemon=True: encerra junto com o programa)
    thread_palma = threading.Thread(target=detectar_palma, daemon=True)
    thread_palma.start()

    falar("Olá, senhor. Como posso ajudá-lo?")

    while True:
        # ----- Captura de entrada -----
        comando = None

        # Tenta primeiro pelo microfone
        try:
            comando = ouvir_microfone()
        except Exception as e:
            # Se o microfone falhar (ex: não tem microfone conectado),
            # cai silenciosamente pro modo texto
            print(f"[AVISO] Microfone indisponível: {e}")

        # Se não ouviu nada pelo microfone, usa o teclado como fallback
        if not comando:
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

        # ----- Processa com Claude -----
        print(f"[Processando] '{comando}'")
        acao = obter_comando_claude(comando)

        if acao is None:
            falar("Não entendi o comando, tente novamente.")
            continue

        # ----- Executa a ação -----
        executar_acao(acao)


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
