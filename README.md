# Limpar metadados de IA

Uma skill para agentes de IA (Claude Code e compatíveis) que tira os metadados de fotos e vídeos antes de publicar ou enviar: credencial C2PA (Credenciais de Conteúdo), XMP, IPTC, EXIF com localização, modelo do aparelho e data, comentários, capítulos e tags.

Você diz quais arquivos. Ela limpa **todos**, sem triagem, e devolve um boletim com o antes e o depois de cada um. Vale igual para foto real, foto retocada e imagem gerada por IA. O original nunca é alterado: a cópia limpa sai com o sufixo `_limpo`, e nunca em cima de um arquivo pedido.

## Usar

Dentro do agente, depois de instalar:

```
/limpar-metadados-ia foto1.jpg capa.png video.mp4
```

Ou direto no terminal, sem agente:

```
python limpar_metadados.py analisar foto1.jpg capa.png video.mp4
python limpar_metadados.py limpar foto1.jpg capa.png video.mp4 --saida limpos
```

`analisar` só lê e mostra as marcas. `limpar` grava as cópias e termina com `Resumo: N de M arquivo(s) limpo(s)`.

## O que ela faz em cada formato

| Formato | Como limpa | Imagem e som |
|---|---|---|
| JPG | tira EXIF, XMP, C2PA, IPTC, comentários e o que vier depois do fim da imagem | pixels idênticos |
| JPG girado pelo EXIF (foto de celular em pé) | gira os pixels e recodifica em qualidade 97 | perda mínima |
| PNG | mantém só os blocos de imagem e cor | pixels idênticos |
| MP4 e MOV | remonta com o ffmpeg sem recodificar, sem tags, capítulos, capa embutida e caixas `uuid` | mesmos dados de vídeo e de áudio, conferidos contra o original |

Outros formatos (HEIC, AVIF, WebP, GIF, TIFF, MKV, WebM) não são aceitos. Converta antes. Atenção ao HEIC, que é o padrão de foto do iPhone: exporte como JPG.

## Instalar

Copie esta pasta para `~/.claude/skills/limpar-metadados-ia` e abra uma sessão nova. Com o git:

```
git clone https://github.com/Guiair21/limpar-metadados-ia ~/.claude/skills/limpar-metadados-ia
```

Precisa de Python 3.8 ou mais novo, da biblioteca Pillow 9.1 ou mais nova (`pip install pillow`) e, para vídeo, do `ffmpeg` e do `ffprobe` no PATH (os dois vêm juntos). No Mac e no Linux o comando costuma ser `python3` no lugar de `python`.

Para conferir que está funcionando:

```
python testes/testar.py
```

## Limites

- A limpeza mexe só nos metadados. Marca d'água invisível nos pixels (como o SynthID) e os classificadores das plataformas continuam lá, então o rótulo de IA pode aparecer mesmo com o arquivo limpo.
- Texto gravado dentro do próprio codec do vídeo ou dos dados comprimidos da imagem não é tocado.
- Se o arquivo passar de novo por um editor depois da limpeza, a marca pode voltar. Rode por último.
- Vídeo cortado sem recodificar (o aplicativo só escondeu o trecho, que continua dentro do arquivo) é recusado, porque a limpeza traria o trecho de volta. Exporte o vídeo de novo e limpe o arquivo exportado.
- Vídeo HDR de iPhone em `.MOV` perde o registro do Dolby Vision e as caixas de cor do Apple Log. Os quadros são os mesmos e a cor básica (HLG) continua indicada.
- O vídeo limpo é conferido contra o original (mesmos dados, mesmos parâmetros de decodificação, sem erro novo). Se a conferência falhar, o arquivo é recusado em vez de sair alterado. Isso aconteceu num MOV de teste com lista de edição fora do comum.
- O tempo dos quadros não é conferido um a um. Quando vídeo e áudio começam depois do zero, a cópia começa no zero (a distância entre os dois se mantém). Uma lista de edição com velocidade diferente de 1 volta para a velocidade normal. Em vídeo de celular, a cópia pode ter um ou dois pacotes de áudio a menos no fim, que já não tocavam.
- O perfil de cor (ICC) é mantido inteiro, com o nome que tiver.
- A regra de cada plataforma sobre avisar conteúdo feito com IA continua valendo para quem publica.

Antes de instalar, leia o `SKILL.md`. A skill roda um único script local, `limpar_metadados.py`, que não acessa a internet e não envia nada para fora da máquina.
