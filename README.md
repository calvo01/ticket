# YesChef — Alertas de Etiquetas (MVP)

MVP para controle interno:
- Cadastro de kitchens (clientes) e parâmetros (alert_days, avg_window_days)
- Lançamentos de estoque: INITIAL / PURCHASE / ADJUST (pode ser negativo)
- Importação manual do consumo via CSV exportado do Metabase
- Painel com lista de CRÍTICO / ALERTA / OK

## Regras (conforme combinado)
- Se a kitchen não tiver um lançamento INITIAL, o sistema assume saldo 0 e coloca em ALERTA.
- O consumo vem do Metabase (CSV), colunas obrigatórias: `kitchen_id`, `day` (YYYY-MM-DD), `labels_used`.
- Recomendação de query no Metabase:

```sql
SELECT
  l.kitchen_id,
  to_char(date_trunc('day', lp.created_at)::date, 'YYYY-MM-DD') AS day,
  COUNT(*)::int AS labels_used
FROM labelprint lp
JOIN label l ON l.id = lp.label_id
WHERE 1=1
  [[ AND lp.created_at >= {{start}} ]]
  [[ AND lp.created_at <  {{end}} ]]
GROUP BY 1, 2
ORDER BY day DESC;
```

## Como rodar localmente (para testes)
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Abra: http://127.0.0.1:8000

## Banco
- SQLite (arquivo `app.db` na mesma pasta).
- Para apontar outro caminho:
```bash
export APP_DB_PATH=/caminho/para/app.db
```


## UX (operador)
- Interface em passos (1→4) para uso sem treinamento.
- Datas aceitas: YYYY-MM-DD ou DD-MM-YYYY.
- Mensagens amigáveis na tela (sem 'Internal Server Error').

## v3 — Auto-cadastro de kitchens via CSV
- O upload do CSV cria automaticamente kitchens novas em `customers`.
- Você NÃO precisa cadastrar kitchen manualmente.
- O cadastro manual continua existindo para completar Nome/CNPJ e ajustar parâmetros.

### CSV recomendado (Metabase)
Colunas mínimas: `kitchen_id`, `day`, `labels_used`
Opcional (recomendado): `kitchen_name`

Exemplo de query:
```sql
SELECT
  k.id AS kitchen_id,
  k.name AS kitchen_name,
  to_char(date_trunc('day', lp.created_at)::date, 'YYYY-MM-DD') AS day,
  COUNT(*)::int AS labels_used
FROM labelprint lp
JOIN label l ON l.id = lp.label_id
JOIN kitchen k ON k.id = l.kitchen_id
WHERE 1=1
  [[ AND lp.created_at >= {{start}} ]]
  [[ AND lp.created_at <  {{end}} ]]
GROUP BY 1, 2, 3
ORDER BY day DESC;
```
