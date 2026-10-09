# Pix com ProfanusPay

Este pacote adapta seu HTML e inclui o servidor que faltava. O preço padrão é R$ 20,00.

## O que foi implementado

- Criação de cobrança na API oficial, exibindo QR Code e Pix copia e cola.
- Preço definido pelo servidor, sem aceitar alterações pelo navegador.
- Consulta periódica do pagamento; apenas `paid` libera o vídeo.
- Validação do ID da transação e do valor bruto antes de liberar o acesso.
- Cobrança recuperada no mesmo navegador após recarregar a página.
- Idempotência com `external_id`: uma tentativa que falhar pode ser repetida sem duplicar a cobrança.
- Tratamento de Pix expirado, cancelado e falhas do gateway.
- Vídeo completo privado, liberado somente para o navegador que criou a cobrança.
- SQLite para persistir pedidos e suporte a streaming com requisições Range.

A chave de API não aparece no HTML. O servidor só serve arquivos públicos específicos. A indicação de vídeo gravado foi mantida.

## 1. Coloque seus arquivos

Dentro da pasta do projeto:

| Arquivo | Destino |
| --- | --- |
| Foto do perfil | `public/perfil.jpg` |
| Vídeo de apresentação gratuito | `public/video.mp4` |
| Vídeo completo pago | `private/completo.mp4` |

Os arquivos de mídia não foram enviados junto com o código original e não estão neste pacote. A criação de Pix fica bloqueada enquanto o vídeo completo não existir, para evitar cobrar por um conteúdo indisponível.

Não coloque o vídeo completo em `public/`. Não reutilize o vídeo pago como prévia.

## 2. Configure o gateway

Copie `.env.example` para `.env` e preencha:

```dotenv
PROFANUS_API_KEY=SUA_CHAVE_REAL
PUBLIC_URL=http://localhost:8080
PIX_AMOUNT=20.00
PIX_EXPIRATION=1800
```

Obtenha a chave em Dashboard → Integrações → API Keys da ProfanusPay. Preencha apenas no servidor. Não envie a chave para conversas, não coloque no HTML e não publique `.env`.

A documentação da ProfanusPay indica a base `https://nexuspag.com`, o header `x-api-key`, `POST /api/pix/create` e `GET /api/pix/{id}`. O valor é enviado em reais, não em centavos. A criação retorna `transaction`; a consulta retorna os campos da transação diretamente.

Documentação consultada em 09/10/2026:
- https://profanuspay.com/docs
- https://profanuspay.com/docs/create-pix
- https://profanuspay.com/docs/get-pix

## 3. Execute localmente

Requer Python 3.11 ou superior. Para desenvolvimento, não há dependências adicionais:

```bash
cd profanus-pix
cp .env.example .env
# Edite .env e coloque os três arquivos de mídia.
python server.py
```

Abra **http://localhost:8080**. Não abra `index.html` com duplo clique: o Pix precisa do servidor. Use localhost, pois a proteção de origem exige correspondência exata com `PUBLIC_URL`.

A API configurada é real. Depois que uma chave válida for instalada, gerar Pix cria uma cobrança real. Não foi identificado um ambiente de testes na documentação consultada.

## 4. Hospede

Precisa de uma hospedagem que execute Python, com HTTPS e volume persistente. Hospedagem apenas de HTML não executa esta integração.

```bash
python -m pip install -r requirements.txt
# No ambiente da hospedagem, execute dentro da pasta do projeto:
gunicorn --workers 2 --threads 4 --timeout 60 --bind 0.0.0.0:8080 server:app
```

Configure as variáveis no painel da hospedagem. Em produção:

```dotenv
PUBLIC_URL=https://seu-dominio.com
DATABASE_PATH=/volume-persistente/orders.sqlite3
FULL_VIDEO_PATH=/volume-privado/completo.mp4
```

A origem deve ser exata, sem barra final, sem subpasta e sem parâmetros. Configure proxy HTTPS, limite de requisições para `/api/pix/criar` e volume persistente para o SQLite. Use uma única instância com disco local persistente; este projeto não está preparado para múltiplas instâncias com discos separados nem para execução serverless. O servidor local de `python server.py` é apenas para desenvolvimento.

Os cookies serão `Secure` quando `PUBLIC_URL` começar com HTTPS. O proxy precisa encaminhar pedidos para o servidor sem publicar a raiz do projeto como pasta estática.

A confirmação usa consulta automática a cada 4 segundos, com breve cache no servidor. Não exige configurar webhook; segundo a documentação, consultar uma cobrança pendente verifica o pagamento no gateway. Falhas de rede adiam a liberação até a consulta funcionar.

## Personalize

Nome, foto, prévia, legendas e textos ficam em `public/index.html`, no objeto `CONFIG`. O preço vem de `PIX_AMOUNT`, no servidor; reinicie após mudar essa variável. A duração do Pix vem de `PIX_EXPIRATION`, em segundos.

O pedido pertence ao cookie do navegador. Limpar cookies ou trocar de dispositivo não recupera o acesso automaticamente. Para acesso entre dispositivos, é necessário acrescentar login ou uma recuperação de compra verificada. Reproduzir um vídeo pago não impede gravações de tela ou cópias feitas por um cliente autorizado.

## Validação realizada

Execute:

```bash
python -m unittest -v test_server
```

11 testes passaram com a API simulada: preço protegido, formato da criação, pendente/pago, streaming, acesso de outro navegador, retomada, idempotência após timeout, valor divergente, expirado/cancelado, proteção de origem, vídeo ausente, arquivos privados e persistência.

Ainda falta validar uma cobrança com sua conta real, sua chave, seus vídeos e a hospedagem escolhida. Nenhuma cobrança real foi criada nesta implementação.
