# Render gratuito + Supabase gratuito + ProfanusPay

Este pacote substitui a versão anterior. Não exige Persistent Disk. Os pedidos ficam no PostgreSQL do Supabase e o vídeo pago fica em um bucket privado. O servidor Python no Render confirma o Pix e libera um link temporário do vídeo.

## 1. Crie seu projeto Supabase

1. Acesse https://supabase.com/dashboard e crie um projeto no plano Free.
2. Escolha e guarde a senha do banco de dados.
3. Abra **SQL Editor > New query**.
4. Cole todo o conteúdo de `supabase.sql` e clique em **Run**.

Isso cria a tabela `pix_orders`, com acesso público bloqueado. Não habilite políticas públicas para essa tabela. Os pedidos são consultados somente pelo backend.

## 2. Envie o vídeo completo de 10 MB

1. No Supabase, abra **Storage** e crie um bucket chamado `videos-pagos`.
2. Mantenha **Public bucket DESATIVADO**. Ele deve ser privado.
3. Faça upload do vídeo com o nome `completo.mp4`, diretamente na raiz do bucket.

O código recusa criar uma cobrança se o vídeo não estiver acessível pelo servidor ou o bucket estiver público. A chave secreta fica somente no Render. O navegador não recebe a chave nem acesso direto ao banco.

## 3. Obtenha as configurações do Supabase

- **SUPABASE_URL:** a Project URL do projeto, como `https://SEUPROJETO.supabase.co`.
- **SUPABASE_SECRET_KEY:** chave secreta de servidor, geralmente começando com `sb_secret_`, em Settings > API Keys. Não use `sb_publishable_` nem `anon`.
- Se usar o modelo antigo de chaves, preencha **SUPABASE_SERVICE_ROLE_KEY** com a chave `service_role`, em vez de SUPABASE_SECRET_KEY.
- **DATABASE_URL:** no topo do projeto, clique em **Connect**, escolha **Session pooler** e copie a URI. Substitua `[YOUR-PASSWORD]` pela senha do banco. Use os nomes e o host fornecidos pelo painel, sem inventar o endereço.

Formato ilustrativo:

```text
postgresql://postgres.SEUPROJETO:SENHA@HOST_DO_POOLER:5432/postgres
```

Caracteres reservados na senha precisam ser codificados na URI: `@` vira `%40`, `#` vira `%23`, `%` vira `%25`, `/` vira `%2F`. Uma senha gerada com letras, números, hífen e sublinhado evita esse problema. Não compartilhe a senha nem a URI completa.

Use Session pooler para ter compatibilidade IPv4 sem comprar o adicional IPv4 do Supabase. O servidor exige SSL na conexão.

## 4. Substitua o projeto no GitHub

Extraia o ZIP. Envie o **conteúdo** da pasta `profanus-pix-gratuito` para seu repositório privado.

Na raiz devem ficar:

```text
server.py
requirements.txt
supabase.sql
render.yaml
.python-version
public/index.html
```

Não envie o ZIP como único arquivo. Não envie `.env`, credenciais ou o vídeo pago. Preserve sua foto e sua prévia, caso já as tenha colocado no projeto:

- `public/perfil.jpg`: foto do perfil.
- `public/video.mp4`: prévia gratuita.

Se você personalizou o HTML da versão anterior, copie suas alterações de nome, legendas, foto e prévia para o novo `public/index.html`.

O conteúdo da pasta `private` e as variáveis `FULL_VIDEO_PATH` e `DATABASE_PATH` não são utilizados nesta versão. Pedidos antigos do SQLite não são migrados automaticamente.

## 5. Configure seu serviço Render existente

Em **Settings**:

| Campo | Valor |
| --- | --- |
| Instance Type | Free |
| Root Directory | Vazio, se server.py estiver na raiz |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn --workers 1 --threads 4 --timeout 90 --bind 0.0.0.0:$PORT server:app` |
| Health Check Path | `/health` |

Se enviou a pasta inteira e `server.py` ficou dentro dela, use `profanus-pix-gratuito` em Root Directory. O arquivo `.python-version` seleciona Python 3.12.12; alternativamente configure `PYTHON_VERSION=3.12.12` no Render.

Em **Environment**, adicione:

| Variável | O que preencher |
| --- | --- |
| `PROFANUS_API_KEY` | Sua API key da ProfanusPay |
| `PUBLIC_URL` | A URL HTTPS exata do seu serviço Render, sem barra final |
| `DATABASE_URL` | URI do Session pooler do Supabase, com senha preenchida |
| `SUPABASE_URL` | Project URL do Supabase |
| `SUPABASE_SECRET_KEY` | Chave secreta do servidor |
| `PIX_AMOUNT` | `20.00` |
| `VIDEO_BUCKET` | `videos-pagos` |
| `VIDEO_PATH` | `completo.mp4` |

A alternativa legada `SUPABASE_SERVICE_ROLE_KEY` é aceita quando SUPABASE_SECRET_KEY não está definida. Não exponha nenhuma das duas no HTML.

Exemplo de PUBLIC_URL:

```text
https://whatsapp-abc123.onrender.com
```

Exclua as antigas `DATABASE_PATH` e `FULL_VIDEO_PATH`. Nenhum disco persistente precisa ser adicionado. Clique em **Manual Deploy > Deploy latest commit**.

Se preferir criar um serviço novo, o `render.yaml` também permite usar **New > Blueprint**, desde que o arquivo esteja na raiz do repositório. Escolha só um caminho: serviço existente ou Blueprint.

## 6. Confira antes de compartilhar

1. Abra o endereço do Render; ele exibirá sua página.
2. Atenda à apresentação e abra a tela de pagamento.
3. Clique em Gerar Pix.
4. Confira se o QR Code e o copia e cola aparecem e se a cobrança foi criada na sua conta ProfanusPay.
5. Valide um Pix pago na sua conta: somente após a API retornar `paid`, o vídeo deve abrir.
6. Recarregue no mesmo navegador e confira a retomada do pedido.

Use uma cobrança real apenas quando estiver preparado para efetuar o teste. A API configurada é de produção. Nenhuma cobrança real foi criada na preparação deste pacote.

## Como o vídeo é liberado

A página consulta `/api/pix/status` a cada 4 segundos. O servidor consulta a ProfanusPay e valida transação e valor. Após `paid`, a página acessa `/api/video`; o servidor verifica o navegador do pedido e redireciona para uma URL assinada do bucket privado. O link dura 1 hora, configurável por `VIDEO_LINK_SECONDS`.

Quem tiver um link assinado válido poderá usá-lo até expirar. Ele não é uma proteção contra gravações de tela ou compartilhamento por um cliente autorizado. Ao expirar, recarregue a página e abra seu pedido novamente no mesmo navegador para receber um link novo. Limpar cookies ou mudar de dispositivo exige recuperação de compra, ainda não incluída.

## Limites gratuitos

Consultados em 09/10/2026:

- Supabase Free: 500 MB de banco, 1 GB de arquivos, máximo 50 MB por arquivo, 5 GB de tráfego não cacheado e 5 GB de tráfego cacheado. Seu vídeo de 10 MB cabe no limite por arquivo. Reproduções repetidas consomem tráfego, portanto a quantidade de espectadores não é ilimitada.
- Projetos Supabase Free podem pausar após uma semana sem atividade.
- Render Free dorme após 15 minutos sem acessos; o próximo acesso pode levar cerca de um minuto para carregar.
- As tarifas da ProfanusPay continuam sendo aplicadas conforme sua conta.

Não há promessa de custo zero fora das cotas gratuitas nem de disponibilidade contínua. Para começar com baixo volume, essa configuração dispensa o disco pago. A alteração foi preparada e testada localmente; sua conta Supabase e sua hospedagem ainda precisam ser configuradas.

## Testes e execução local

```bash
python -m pip install -r requirements.txt
python -m unittest -v test_server
```

15 testes passaram com gateway e serviços externos simulados. Eles verificam criação, valor protegido, confirmação, retomada, timeout com idempotência, acesso por outro navegador, valor divergente, expirado/cancelado, origem da requisição, persistência lógica, bucket público recusado, vídeo ausente, URL assinada e rejeição de URL externa.

Os testes usam um banco local de teste para verificar o fluxo; não conectam ao PostgreSQL do seu projeto nem fazem pagamentos reais. A conexão ao Supabase e a validação real do Pix devem ser conferidas com as configurações acima.

Para executar localmente, copie `.env.example` para `.env`, preencha com suas credenciais e execute `python server.py`. Abra `http://localhost:8080`; não abra o HTML por duplo clique.

## Referências oficiais

- https://profanuspay.com/docs/create-pix
- https://profanuspay.com/docs/get-pix
- https://supabase.com/docs/guides/database/connecting-to-postgres
- https://supabase.com/docs/guides/api/api-keys
- https://supabase.com/docs/reference/python/storage-from-createsignedurl
- https://supabase.com/pricing
- https://render.com/docs/free
