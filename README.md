# J.A.R.V.I.S. - Assistente de Desktop Pessoal

> **Just A Rather Very Intelligent System**

Assistente de desktop inspirado no J.A.R.V.I.S. do Homem de Ferro, controlado por voz e texto. Desenvolvido inteiramente com auxílio de Inteligência Artificial.

---

## Funcionalidades

- **Controle por voz** — fala um comando e o Jarvis executa
- **Controle por texto** — digita no terminal se preferir
- **Voz realista** — integração com Fish Audio para TTS de alta qualidade
- **Personalidade do filme** — responde como o Jarvis original, chamando de "senhor"
- **Abrir aplicativos** — Chrome, Calculadora, Bloco de Notas, VS Code, Spotify e mais
- **Pesquisar na web** — Google e YouTube por voz
- **Abrir sites** — qualquer URL por voz
- **Abrir projetos no VS Code** — abre pastas de projetos diretamente
- **Bom dia inteligente** — fala a hora atual e o clima de Goiânia ao ser cumprimentado
- **Integração com Kronos** — lê suas tarefas pendentes do dia direto do Supabase
- **Detecção de palma** — duas palmas rápidas tocam AC/DC no YouTube automaticamente

---

## Tecnologias

| Tecnologia | Uso |
|---|---|
| [Claude Haiku](https://anthropic.com) | Cérebro do assistente — interpreta comandos |
| [Fish Audio](https://fish.audio) | Text-to-speech com voz realista |
| [Edge TTS](https://github.com/rany2/edge-tts) | Fallback de voz gratuito |
| [SpeechRecognition](https://pypi.org/project/SpeechRecognition/) | Reconhecimento de voz (Google Speech API) |
| [Supabase](https://supabase.com) | Integração com banco de dados do Kronos |
| [sounddevice](https://python-sounddevice.readthedocs.io) | Captura de áudio e detecção de palma |
| [wttr.in](https://wttr.in) | Previsão do tempo gratuita |

---

## Instalação

### 1. Clone o repositório

```bash
git clone https://github.com/seu-usuario/jarvis.git
cd jarvis
```

### 2. Instale as dependências

```bash
pip install -r requirements.txt
```

### 3. Configure as variáveis de ambiente

Copie o arquivo de exemplo e preencha com suas chaves:

```bash
cp .env.example .env
```

Edite o `.env`:

```env
ANTHROPIC_API_KEY=sua-chave-aqui
FISH_AUDIO_API_KEY=sua-chave-aqui
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=sua-anon-key
SUPABASE_SERVICE_KEY=sua-service-role-key
SUPABASE_USER_ID=seu-user-id
```

### 4. Execute

```bash
python jarvis.py
```

---

## Comandos de exemplo

| O que falar | O que acontece |
|---|---|
| *"Bom dia"* | Jarvis fala a hora e o clima atual |
| *"Quais minhas tarefas de hoje?"* | Lista tarefas do Kronos |
| *"Abre o Chrome"* | Abre o Google Chrome |
| *"Abre a pasta Kronos"* | Abre o projeto no VS Code |
| *"Pesquisa Python no YouTube"* | Abre busca no YouTube |
| *"Pesquisa previsão do tempo"* | Abre busca no Google |
| *"Abre github.com"* | Abre o site no navegador |
| Duas palmas rápidas | Toca AC/DC no YouTube |
| *"Tchau"* | Encerra o Jarvis |

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `ANTHROPIC_API_KEY` | Sim | Chave da API da Anthropic |
| `FISH_AUDIO_API_KEY` | Não | Chave do Fish Audio para voz realista |
| `SUPABASE_URL` | Não | URL do projeto Supabase |
| `SUPABASE_SERVICE_KEY` | Não | Service role key do Supabase |
| `SUPABASE_USER_ID` | Não | ID do usuário no Supabase |

---

## Observações

- O microfone precisa estar conectado para o modo de voz funcionar
- Sem `FISH_AUDIO_API_KEY`, o Jarvis usa o Edge TTS como fallback (gratuito)
- Sem as credenciais do Supabase, a integração com o Kronos fica desativada
- A detecção de palma pode precisar de ajuste de sensibilidade dependendo do microfone

---

## Feito com IA

Este projeto foi desenvolvido inteiramente com auxílio do **Claude** (Anthropic), utilizando o **Claude Code** como assistente de programação. Desde a arquitetura inicial até cada funcionalidade, o desenvolvimento foi guiado por conversas com IA.

> *"À sua disposição, senhor."* — J.A.R.V.I.S.
