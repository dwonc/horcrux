# Debate Chain 환경 세팅 가이드

팀원용 초기 설치 가이드입니다. Windows / macOS 모두 지원합니다.

## 1. 사전 요구사항

### Python 3.11+
```bash
# 설치 확인
python3 --version   # macOS
python --version    # Windows

# 없으면
# macOS: brew install python3
# Windows: https://python.org 에서 다운로드
```

### Node.js 18+
```bash
node --version

# 없으면
# macOS: brew install node
# Windows: https://nodejs.org 에서 LTS 다운로드
```

## 2. AI CLI 설치

### Claude Code CLI (필수)
```bash
npm install -g @anthropic-ai/claude-code
```
- **구독 필요**: Claude Max ($100/월) 또는 Claude Pro ($20/월)
- 인증:
```bash
claude
# 브라우저 열리면 Anthropic 계정으로 로그인
```
- 테스트:
```bash
claude -p "say hello"
```

### Codex CLI (필수)
```bash
npm install -g @openai/codex
```
- **구독 필요**: ChatGPT Plus ($20/월) 이상
- 인증:
```bash
codex auth login
# 브라우저 열리면 OpenAI 계정으로 로그인
```
- 테스트:
```bash
codex exec "say hello" --skip-git-repo-check
```

### Gemini CLI (선택)
```bash
npm install -g @google/gemini-cli
```
- **무료** (Google 계정 로그인만 필요, 할당량 제한 있음)
- 또는 API 키 방식:
```bash
# Google AI Studio에서 키 발급: https://aistudio.google.com/apikey

# macOS
export GEMINI_API_KEY=your_key_here

# Windows
set GEMINI_API_KEY=your_key_here
```
- 테스트:
```bash
gemini -p "say hello"
```

> **구독 없이 쓸 수 있나요?**
> - Claude CLI: 구독 필수 (Generator/Synthesizer 역할)
> - Codex CLI: 구독 필수 (Critic 역할)
> - Gemini CLI: 무료 가능 (Verifier 역할, 할당량 제한)

## 3. 프로젝트 설치

```bash
# 클론
git clone https://github.com/your-username/debate-chain.git
cd debate-chain

# Python 의존성
pip install -r requirements.txt     # Windows
pip3 install -r requirements.txt    # macOS

# Node.js 의존성 (MCP 서버용)
npm install

# 환경변수 설정
cp .env.example .env
# .env 파일 열어서 GEMINI_API_KEY 입력
```

## 4. 실행

### Windows
```powershell
# 방법 A: 더블클릭
start.bat

# 방법 B: 터미널
cd debate-chain
python server.py
```

### macOS
```bash
cd debate-chain
python3 server.py
# → http://localhost:5000
```

### Claude Desktop MCP 연동 (선택)

MCP 설정 파일 위치:
```
# Windows
%APPDATA%\Claude\claude_desktop_config.json

# macOS
~/Library/Application Support/Claude/claude_desktop_config.json
```

아래 내용 추가:
```json
{
  "mcpServers": {
    "debate-chain": {
      "command": "node",
      "args": ["/절대경로/debate-chain/mcp_server.js"]
    }
  }
}
```

> **macOS 경로 예시**: `"/Users/dongwon/debate-chain/mcp_server.js"`
> **Windows 경로 예시**: `"D:\\Custom_AI-Agent_Project\\debate-chain\\mcp_server.js"`

Claude Desktop 재시작 후, 대화에서 자연어로 사용:
- "이 코드 리뷰해줘" → debate 모드
- "JWT 인증 만들어줘" → pair2 모드

## 5. 연결 테스트

서버 실행 후 브라우저에서:
```
http://localhost:5000
→ 🔧 연결 테스트 버튼 클릭
→ claude ✅, codex ✅, gemini ✅ 확인
```

또는 터미널에서:
```bash
curl http://localhost:5000/api/test
```

## 6. 사용법

### Debate 모드 (코드 리뷰)
1. 웹 UI 텍스트박스에 리뷰할 코드 또는 태스크 입력
2. Enter로 실행 (Shift+Enter = 줄바꿈)
3. Generator → Critic → Verifier → (수렴 안 되면 반복)
4. 수렴(8.0/10) 되면 결과 Copy

### Pair2 모드 (2-AI 병렬 생성)
- MCP: "JWT 인증 pair2로 만들어줘"
- API: `POST /api/pair` body: `{"task": "...", "mode": "pair2"}`

### Pair3 모드 (3-AI 병렬 생성)
- MCP: "추천 시스템 pair3로 만들어줘"
- API: `POST /api/pair` body: `{"task": "...", "mode": "pair3"}`

## 7. 트러블슈팅

### Claude CLI 인증 만료
```bash
claude auth login
```

### Codex CLI 인증 만료
```bash
codex auth login
```

### Gemini 할당량 소진
- 자동으로 다음 모델로 폴백 (gemini-2.5-flash → 2.0-flash → ...)
- 약 14시간 후 리셋
- 구독하면 할당량 증가

### MCP 서버 안 보임
1. config 파일 경로 확인 (위 4번 참조)
2. Claude Desktop 완전 종료 → 재시작
3. 로그 확인:
```bash
# Windows
type %APPDATA%\Claude\logs\mcp-server-debate-chain.log

# macOS
cat ~/Library/Application\ Support/Claude/logs/mcp-server-debate-chain.log
```

### 서버 포트 충돌
```bash
# Windows
netstat -ano | findstr :5000

# macOS
lsof -i :5000
kill -9 {PID}
```

### macOS에서 python 명령 안 됨
```bash
# macOS는 python3 사용
python3 server.py

# 또는 alias 설정
echo 'alias python=python3' >> ~/.zshrc
source ~/.zshrc
```

## 8. API 레퍼런스

| 메서드 | 엔드포인트 | 설명 |
|--------|-----------|------|
| POST | `/api/start` | Debate 시작 |
| GET | `/api/status/{id}` | 상태 조회 |
| POST | `/api/stop/{id}` | 중지 |
| GET | `/api/threads` | 스레드 목록 |
| DELETE | `/api/delete/{id}` | 삭제 |
| GET | `/api/test` | 연결 테스트 |
| POST | `/api/pair` | Pair 모드 시작 |
| GET | `/api/pair/status/{id}` | Pair 상태 조회 |
| GET | `/api/gemini-models` | Gemini 모델 상태 |
