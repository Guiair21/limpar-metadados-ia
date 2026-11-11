---
name: limpar-metadados-ia
description: Use quando o usuário for publicar foto, carrossel ou Reel e quiser tirar os metadados de IA do arquivo (C2PA, Credenciais de Conteúdo, XMP, IPTC, EXIF) para o Instagram não colocar o rótulo "Info de IA" por causa de marca que sobrou de ferramenta de edição. Dispare com "/limpar-metadados-ia", "limpa os metadados", "tira a marca de IA", "o Instagram marcou como IA", "antes de postar limpa isso", "remove o C2PA". Analisa e limpa JPG, PNG, MP4 e MOV (sem recodificar, salvo foto de celular girada pelo EXIF). Responda sempre em português.
---

# Limpar metadados de IA antes de publicar

## Por que existe
Photoshop (Preenchimento Generativo), Firefly, Canva, CapCut, Veo, Gemini e outras ferramentas gravam dentro do arquivo uma credencial (C2PA) e campos IPTC/XMP dizendo "IA". O Instagram lê isso e põe o rótulo "Info de IA". Se a peça é a imagem do usuário e a IA só tratou, o arquivo carrega uma marca que conta uma história maior do que a verdadeira. A skill tira a marca do arquivo; ela não muda um pixel.

## Regra de honestidade (vem antes de limpar)
Peça tratada por IA é "parcialmente gerada por IA", **nunca escondida**. Então, antes de limpar, perguntar uma vez por lote:

> "Essa peça tem algum elemento criado por IA (rosto, cenário, objeto ou cena que não existia na gravação), ou é só a sua imagem com retoque, upscale, corte e cor?"

- **Só retoque, upscale, corte, cor, legenda, trilha:** limpar.
- **Tem elemento gerado por IA que parece real** (cenário, pessoa, objeto ou cena fotorrealista criada): **não limpar.** Dizer ao usuário que a política do Instagram pede divulgação nesse caso, e que o jeito certo é ele mesmo ativar o rótulo de IA na publicação. Limpar o arquivo aqui seria esconder.
- **Dúvida:** perguntar de novo, não adivinhar.

## Como usar
Script: `limpar_metadados.py` (nesta pasta)

1. **Analisar** (só lê, não altera nada):
   `python limpar_metadados.py analisar "ARQUIVO1" "ARQUIVO2"`
   Mostra quais marcas existem e qual ferramenta provavelmente colocou (Firefly, Veo, C2PA etc.).
2. **Limpar** (depois do aval da regra acima):
   `python limpar_metadados.py limpar "ARQUIVO" --saida "PASTA"`
   Sem `--saida`, grava em `limpos\` ao lado do original, com sufixo `_limpo`. O original nunca é tocado.
3. **Conferir o boletim**: cada arquivo mostra marcas antes e depois e termina em `[ok]` ou `[x]`. O script também abre o arquivo limpo para confirmar que não está quebrado. Só `[ok]` vai para publicar. `[x]` quer dizer que ainda sobrou marca: não usar o arquivo e avisar o usuário.

## O que cada formato faz
| Formato | Método | Qualidade |
|---|---|---|
| JPG | remove os blocos APP1 (EXIF/XMP), APP11 (C2PA), APP13 (IPTC) e comentários e qualquer coisa depois do fim da imagem principal (miniatura MPF, vídeo de Motion Photo, texto colado); mantém só o JFIF exato, o perfil de cor e o que descreve a imagem | pixels idênticos |
| JPG com rotação EXIF (foto de celular em pé) | gira os pixels, recodifica em qualidade 97 e passa pela mesma limpeza de blocos. Foto em tons de cinza ou CMYK sai em RGB | perda mínima |
| PNG | mantém só os blocos de imagem e cor (lista branca) e corta tudo depois do IEND; some caBX, eXIf, textos, tIME e qualquer bloco desconhecido | pixels idênticos |
| MP4 / MOV | remux do ffmpeg com `-c copy`, sem tags, sem capítulos, sem caixa `uuid`. Só ficam vídeo e áudio: legenda, timecode e outros fluxos são descartados | vídeo e áudio idênticos |

Blocos de tamanho fora do padrão no PNG também são descartados. Efeito colateral: foto "Motion Photo" ou com miniatura embutida perde o vídeo e a miniatura (fica só a imagem principal). De brinde, some também a localização GPS, o modelo da câmera e a data que ficam no EXIF. É bom para privacidade.

## Quando rodar no fluxo
**Por último**, depois do render final e de qualquer reexportação. Se o arquivo passar de novo por Premiere, CapCut, Canva ou Photoshop depois da limpeza, a ferramenta pode gravar a marca outra vez. No `/postar`: limpar o arquivo final e passar o `_limpo` para o postar. Carrossel: limpar cada imagem.

## Limites que o usuário precisa saber
- A detecção usa uma lista fixa de palavras (C2PA, IPTC, nomes de ferramentas). Texto livre de IA fora da lista não aparece no `analisar`; a limpeza, porém, remove os blocos inteiros, não só as palavras achadas.
- **Fora do alcance da limpeza:** texto escondido dentro do fluxo comprimido (os dados de imagem do JPEG, ou mensagens SEI e texto de encoder dentro do codec do vídeo H.264/H.265). O C2PA e o XMP ficam no contêiner e esses saem; o que está no meio do codec não é tocado.
- **HEIC e AVIF** não são aceitos: o script recusa com `[?]`. Converta para JPG antes.
- Quais ferramentas gravam C2PA e como o Instagram reage são coisas que não verifiquei em fonte oficial.
- **Limpar o arquivo não garante que o rótulo some.** O Instagram também usa marca d'água invisível (o Veo e o Gemini embutem SynthID nos pixels) e classificadores próprios. Esses sinais não estão nos metadados e esta skill não os toca.
- O script avisa só sobre metadados. Se o Instagram marcar mesmo assim, o caminho é pedir revisão do rótulo no próprio app.
- Antes de culpar o rótulo por queda de alcance, comparar com o diagnóstico de 29/10 (o gargalo apontado foi o gancho, com 96% do alcance vindo de quem não segue). Rótulo de IA e alcance são coisas separadas até haver teste mostrando o contrário.

## Teste rápido de que a skill está sã
`python limpar_metadados.py analisar` num JPG feito no celular deve dizer "nenhuma marca de IA". Num arquivo exportado do Photoshop com Preenchimento Generativo, deve listar C2PA e IPTC.
