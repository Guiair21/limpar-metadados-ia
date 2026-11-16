---
name: limpar-metadados-ia
description: Use quando o usuário pedir para tirar os metadados de fotos ou vídeos antes de publicar ou enviar (C2PA, Credenciais de Conteúdo, XMP, IPTC, EXIF, GPS), ou quando uma plataforma colocar o rótulo "Info de IA" no que ele postou. Dispare com "/limpar-metadados-ia", "limpa os metadados", "tira a marca de IA", "o Instagram marcou como IA", "antes de postar limpa isso", "remove o C2PA", "tira o EXIF". Aceita JPG, PNG, MP4 e MOV. Responda sempre em português.
---

# Limpar metadados de IA antes de publicar

## Por que existe
Photoshop (Preenchimento Generativo), Firefly, Canva, CapCut, Veo, Gemini e outras ferramentas gravam dentro do arquivo uma credencial (C2PA) e campos IPTC/XMP dizendo "IA". O Instagram lê isso e põe o rótulo "Info de IA". A câmera e o celular gravam também EXIF com localização, modelo do aparelho e data. A skill tira tudo isso do arquivo e não mexe na imagem nem no som. A única exceção é a foto girada pelo EXIF, que precisa ser regravada (está na tabela de formatos).

## Regra do lote: todo arquivo pedido sai limpo
O pedido do usuário já é a decisão. A limpeza é a mesma para foto real, foto retocada e imagem inteira gerada por IA: o script trabalha nos blocos do arquivo, e o conteúdo da imagem não entra na conta. A skill não tem pergunta de triagem. Recebeu a lista, vai direto para o passo 1.

O lote só está pronto quando a linha `Resumo` do script mostra **todos** os arquivos pedidos como limpos. Um arquivo só fica de fora por um destes motivos, sempre dito ao usuário com o nome do arquivo: formato não aceito, arquivo não encontrado (ou pasta no lugar de arquivo), erro na limpeza, ou cópia reprovada na conferência final.

## Como usar
Script: `limpar_metadados.py` (nesta pasta)

1. **Analisar** (só lê, não altera nada):
   `python limpar_metadados.py analisar "ARQUIVO1" "ARQUIVO2"`
   Mostra quais marcas existem e qual ferramenta provavelmente colocou (Firefly, Veo, C2PA etc.). Serve para o "antes" do boletim.
2. **Limpar todos os arquivos pedidos**, de uma vez, inclusive os que a análise mostrou sem marca de IA (eles ainda têm EXIF, localização e tags):
   `python limpar_metadados.py limpar "ARQUIVO1" "ARQUIVO2" --saida "PASTA"`
   Sem `--saida`, grava em `limpos\` ao lado de cada original, com sufixo `_limpo`. O original nunca é tocado, e a cópia nunca cai em cima de um arquivo pedido. Uma cópia `_limpo` de rodada anterior que já esteja na pasta de saída é sobrescrita. Pasta inteira: passe todos os arquivos de foto e vídeo dela, um por um (o script não aceita a pasta como argumento). Os de formato não aceito aparecem no `Resumo` como não limpos.
3. **Conferir o boletim**: cada arquivo mostra marcas antes e depois e termina em `[ok]` ou `[x]`. O script também confere a cópia: a foto precisa abrir, e o vídeo precisa decodificar sem erro novo, com os mesmos dados de vídeo e de áudio e os mesmos parâmetros de decodificação do original. A última linha é o `Resumo: N de M arquivo(s) limpo(s)`, seguida dos que ficaram de fora. Só `[ok]` vai para publicar. `[x]` quer dizer que deu erro ou que sobrou marca depois da limpeza: nos dois casos o script descarta a cópia. Avisar o usuário com o nome do arquivo e o motivo.
4. **Responder com o boletim**: uma linha por arquivo pedido (marcas antes, resultado, caminho do `_limpo`), o resumo do lote e, uma vez, os limites da seção abaixo que se aplicam ao caso.

## O que cada formato faz
| Formato | Método | Qualidade |
|---|---|---|
| JPG | remove os blocos APP1 (EXIF/XMP), APP11 (C2PA), APP13 (IPTC) e comentários e qualquer coisa depois do fim da imagem principal (miniatura MPF, vídeo de Motion Photo, texto colado); mantém só o JFIF exato, o perfil de cor e o que descreve a imagem | pixels idênticos |
| JPG com rotação EXIF (foto de celular em pé) | gira os pixels, recodifica em qualidade 97 e passa pela mesma limpeza de blocos. Foto em tons de cinza ou CMYK sai em RGB | perda mínima |
| PNG | mantém só os blocos de imagem e cor (lista branca) e corta tudo depois do IEND; some caBX, eXIf, textos, tIME e qualquer bloco desconhecido | pixels idênticos |
| MP4 / MOV | remux do ffmpeg com `-c copy`, sem tags, sem capítulos, sem caixa `uuid`. Só ficam vídeo e áudio: legenda, timecode, capa embutida e outros fluxos são descartados | mesmos dados de vídeo e de áudio, conferidos contra o original |

Blocos de tamanho fora do padrão no PNG também são descartados. Efeito colateral: foto "Motion Photo" ou com miniatura embutida perde o vídeo e a miniatura (fica só a imagem principal). De brinde, some também a localização GPS, o modelo da câmera e a data que ficam no EXIF. É bom para privacidade.

## Quando rodar no fluxo
**Por último**, depois do render final e de qualquer reexportação. Se o arquivo passar de novo por Premiere, CapCut, Canva ou Photoshop depois da limpeza, a ferramenta pode gravar a marca outra vez. Se a publicação for feita por outra ferramenta ou skill, é o `_limpo` que segue para ela. Carrossel: limpar cada imagem.

## Limites que o usuário precisa saber
- A detecção usa uma lista fixa de palavras (C2PA, IPTC, nomes de ferramentas). Texto livre de IA fora da lista não aparece no `analisar`; a limpeza, porém, remove os blocos inteiros, não só as palavras achadas.
- **Fora do alcance da limpeza:** texto escondido dentro do fluxo comprimido (os dados de imagem do JPEG, ou mensagens SEI e texto de encoder dentro do codec do vídeo H.264/H.265). O C2PA e o XMP ficam no contêiner e esses saem; o que está no meio do codec não é tocado.
- **Só JPG, PNG, MP4 e MOV.** HEIC, AVIF, WebP, GIF, TIFF, MKV e WebM não são aceitos: o script responde `[?]` e o arquivo conta como não limpo no resumo. Converta para JPG, PNG ou MP4 antes.
- **Vídeo cortado sem recodificar:** alguns aplicativos de corte rápido só escondem o trecho cortado, que continua dentro do arquivo. O remonte traria esse trecho de volta, então o script compara a duração exibida e recusa com `[x]`. O caminho é exportar o vídeo de novo e limpar o arquivo exportado.
- **Vídeo HDR de iPhone em `.MOV`:** o registro do Dolby Vision e as caixas de cor do Apple Log (`colr`, `logs`) não são mantidos no remonte. Os quadros são os mesmos e a cor básica (HLG) continua indicada no próprio fluxo.
- **Vídeo que o remonte alteraria:** se a cópia decodificar com erro que o original não tem, ou sair com dados ou parâmetros de decodificação diferentes, o script recusa com `[x]` e descarta a cópia. Aconteceu num MOV de teste com lista de edição fora do comum; nos testes com vídeos de iPhone (HEVC e H.264) e com vídeo de taxa de quadros variável, não.
- **Tempos do vídeo:** os tempos não são conferidos quadro a quadro. Quando o vídeo e o áudio começam depois do zero, a cópia começa no zero, e a distância entre áudio e vídeo se mantém com diferença de até 1 milissegundo. Uma lista de edição com velocidade diferente de 1 (câmera lenta gravada na lista, raro) volta para a velocidade normal.
- **Áudio de celular:** a cópia pode sair com um ou dois pacotes de áudio a menos no fim (pacotes que já não tocavam) e com alguns milissegundos de diferença no início do áudio. A sincronia não muda.
- **PNG com orientação gravada no bloco eXIf:** o bloco sai e os pixels não são girados (no JPG, são). Raro.
- **O perfil de cor (ICC) é mantido inteiro**, com o nome que tiver. Ele é dado de cor, e a conferência final não procura palavras nele.
- Quais ferramentas gravam C2PA e como o Instagram reage são coisas que não verifiquei em fonte oficial.
- **Limpar o arquivo não garante que o rótulo some.** O Instagram também usa marca d'água invisível (o Veo e o Gemini embutem SynthID nos pixels) e classificadores próprios. Esses sinais não estão nos metadados e esta skill não os toca.
- O script avisa só sobre metadados. Se o Instagram marcar mesmo assim, o caminho é pedir revisão do rótulo no próprio app.
- A regra de cada plataforma sobre avisar conteúdo feito com IA continua valendo para quem publica. A limpeza mexe no arquivo, não nessa obrigação.
- Rótulo de IA e queda de alcance são coisas separadas até haver teste mostrando o contrário.

## Requisitos
Python 3.8 ou mais novo, a biblioteca Pillow 9.1 ou mais nova (`pip install pillow`) e, para vídeo, o `ffmpeg` e o `ffprobe` no PATH (os dois vêm juntos).

## Teste rápido de que a skill está sã
`python testes/testar.py` cria arquivos de teste com marcas, limpa e confere o resultado. À mão: `python limpar_metadados.py analisar` num JPG feito no celular deve dizer "nenhuma marca de IA"; num arquivo exportado do Photoshop com Preenchimento Generativo, deve listar C2PA e IPTC.
