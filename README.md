# 🛒 쇼핑 리스트

Streamlit으로 만든 간단한 쇼핑 리스트 웹앱입니다. 아이템 추가·수정·삭제(되돌리기)·체크 기능을 제공합니다.
자세한 요구사항은 [PRD.md](PRD.md)를 참고하세요.

## 로컬 실행

```bash
pip install -r requirements.txt
streamlit run app.py
```

브라우저에서 http://localhost:8501 로 접속합니다.

## 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

테스트는 임시 파일을 사용하므로 실제 목록(`shopping_list.json`)에는 영향을 주지 않습니다.

## 데이터 저장

- 목록은 `shopping_list.json` 파일에 저장됩니다(Git에는 올라가지 않음).
  환경변수 `SHOPPING_LIST_FILE`로 저장 위치를 바꿀 수 있습니다.
- 여러 탭이나 기기에서 동시에 써도 서로의 변경을 덮어쓰지 않습니다.
- 저장 파일이 손상되면 원본을 `*.corrupt-날짜.json` 또는 `*.backup-날짜.json`으로 보관한 뒤 계속 실행합니다.
- Streamlit Community Cloud에서는 앱이 재시작되면 저장 파일이 초기화될 수 있습니다.
