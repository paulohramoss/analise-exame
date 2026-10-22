/* Three Health — exibição do laudo (resultado, laudo salvo, teste gratuito).
 *
 *   renderLaudoInto(element, markdownText)  // converte o laudo e insere no elemento
 *   stageLabel(stage)                       // texto da etapa da análise em andamento
 *
 * O texto do laudo vem da IA (e pode ser influenciado por prompt injection):
 * ele é ESCAPADO antes de qualquer conversão, e só as tags geradas aqui
 * (h2–h4, p, ul/ol/li, blockquote, strong, em) entram como HTML.
 * Uma implementação só, para a tela e o PDF ficarem iguais nas três páginas.
 */
(function () {
    'use strict';

    function escapeHtml(text) {
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function inline(escaped) {
        return escaped
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g, '$1<em>$2</em>');
    }

    function renderLaudoMarkdown(text) {
        const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
        const out = [];
        let paragraph = [];
        let list = null;        // { tag: 'ul' | 'ol', items: [] }
        let quote = [];

        function flushParagraph() {
            if (paragraph.length) out.push('<p>' + paragraph.map(inline).join('<br>') + '</p>');
            paragraph = [];
        }
        function flushList() {
            if (list) out.push('<' + list.tag + '>' + list.items.map(i => '<li>' + inline(i) + '</li>').join('') + '</' + list.tag + '>');
            list = null;
        }
        function flushQuote() {
            if (quote.length) out.push('<blockquote>' + quote.map(inline).join('<br>') + '</blockquote>');
            quote = [];
        }
        function flushAll() { flushParagraph(); flushList(); flushQuote(); }

        for (const raw of lines) {
            const line = escapeHtml(raw.trim());
            if (!line) { flushAll(); continue; }

            const heading = /^(#{1,4})\s+(.+)$/.exec(line);
            if (heading) {
                flushAll();
                const level = Math.min(heading[1].length + 1, 4);   // # → h2, ## → h3, ### → h4
                out.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>');
                continue;
            }
            const quoted = /^&gt;\s?(.*)$/.exec(line);
            if (quoted) { flushParagraph(); flushList(); quote.push(quoted[1]); continue; }

            const bullet = /^[-*•]\s+(.+)$/.exec(line);
            const numbered = /^\d+[.)]\s+(.+)$/.exec(line);
            if (bullet || numbered) {
                flushParagraph(); flushQuote();
                const tag = bullet ? 'ul' : 'ol';
                if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
                list.items.push((bullet || numbered)[1]);
                continue;
            }
            if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) { flushAll(); continue; }

            flushList(); flushQuote();
            paragraph.push(line);
        }
        flushAll();
        return out.join('\n');
    }

    function renderLaudoInto(element, text) {
        element.innerHTML = renderLaudoMarkdown(text);
    }

    const STAGES = {
        na_fila: 'Na fila — aguardando um analisador livre',
        identificando_regiao: 'Identificando a região anatômica',
        analisando: 'Analisando as imagens com os modelos de IA',
        consenso: 'Gerando o laudo de consenso entre os modelos',
        salvando: 'Salvando o laudo',
    };

    function stageLabel(stage) {
        return STAGES[stage] || '';
    }

    /* Metadados do PDF — os mesmos campos, na mesma ordem, nas três páginas.
       O laudo nunca expõe o fornecedor/modelo de IA usado: sempre "Software próprio". */
    function laudoPdfMeta(exam) {
        exam = exam || {};
        const refs = Number(exam.references_used || 0);
        return [
            { label: 'Região', value: exam.exam_type || '—' },
            { label: 'Imagens analisadas', value: String(exam.num_images || 1) },
            { label: 'Referências usadas', value: refs > 0 ? `${refs} ${refs === 1 ? 'imagem' : 'imagens'}` : 'Sem referência' },
            { label: 'Análise por IA', value: 'Software próprio' },
        ];
    }

    window.laudoPdfMeta = laudoPdfMeta;
    window.renderLaudoMarkdown = renderLaudoMarkdown;
    window.renderLaudoInto = renderLaudoInto;
    window.stageLabel = stageLabel;
})();
