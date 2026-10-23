/* Three Health — marcação dos achados nas imagens do exame.
 *
 *   annotateExamImages(imgs, marcacoes)          // desenha os círculos sobre as imagens
 *   renderFindingsLegend(element, marcacoes, n)  // lista numerada com o local exato
 *   setExamMarksVisible(imgs, visible)           // alterna imagem marcada / original
 *   initExamFindings(imgs, marcacoes, n)         // tudo acima + botão #toggle-marks
 *
 * Verde = forte grau de certeza, amarelo = moderado, vermelho = baixo.
 * As caixas vêm em box_2d [ymin, xmin, ymax, xmax] normalizado de 0 a 1000.
 * A imagem marcada substitui o src da <img>: o PDF, que lê img.src, sai marcado.
 * Todo texto vindo da IA entra por textContent, nunca como HTML.
 */
(function () {
    'use strict';

    const CERTEZA = {
        forte:    { color: '#16a34a', label: 'Forte grau de certeza' },
        moderado: { color: '#eab308', label: 'Moderado grau de certeza' },
        baixo:    { color: '#dc2626', label: 'Baixo grau de certeza' },
    };

    function whenLoaded(img) {
        if (img.complete && img.naturalWidth) return Promise.resolve();
        return new Promise(resolve => {
            img.addEventListener('load', resolve, { once: true });
            img.addEventListener('error', resolve, { once: true });
        });
    }

    function drawMark(ctx, mark, w, h) {
        const style = CERTEZA[mark.certeza];
        if (!style) return;
        const [ymin, xmin, ymax, xmax] = mark.box_2d;
        const short = Math.min(w, h);
        const cx = ((xmin + xmax) / 2000) * w;
        const cy = ((ymin + ymax) / 2000) * h;
        // Folga de 15% para o círculo envolver a estrutura sem cobri-la.
        const rx = Math.max(((xmax - xmin) / 2000) * w * 1.15, short * 0.025);
        const ry = Math.max(((ymax - ymin) / 2000) * h * 1.15, short * 0.025);
        const line = Math.max(2, short * 0.006);

        ctx.beginPath();
        ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
        ctx.lineWidth = line * 2.2;                 // contorno escuro: contraste em RM/RX
        ctx.strokeStyle = 'rgba(0, 0, 0, 0.55)';
        ctx.stroke();
        ctx.lineWidth = line;
        ctx.strokeStyle = style.color;
        ctx.stroke();

        // Número do achado, encostado no círculo e sempre dentro da imagem.
        const badge = Math.max(10, short * 0.028);
        const bx = Math.min(Math.max(cx + rx * 0.72, badge), w - badge);
        const by = Math.min(Math.max(cy - ry * 0.72, badge), h - badge);
        ctx.beginPath();
        ctx.arc(bx, by, badge, 0, Math.PI * 2);
        ctx.fillStyle = style.color;
        ctx.fill();
        ctx.lineWidth = Math.max(1.5, line * 0.6);
        ctx.strokeStyle = '#ffffff';
        ctx.stroke();
        ctx.fillStyle = mark.certeza === 'moderado' ? '#1f2937' : '#ffffff';
        ctx.font = `800 ${Math.round(badge * 1.15)}px Nunito, Arial, sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(mark.numero), bx, by + badge * 0.05);
    }

    async function annotateExamImages(imgs, marcacoes) {
        const marks = Array.isArray(marcacoes) ? marcacoes : [];
        await Promise.all(Array.from(imgs).map(async (img, index) => {
            const own = marks.filter(m => m.imagem === index + 1 && CERTEZA[m.certeza]);
            if (!own.length) return;
            await whenLoaded(img);
            const w = img.naturalWidth, h = img.naturalHeight;
            if (!w || !h) return;
            const canvas = document.createElement('canvas');
            canvas.width = w;
            canvas.height = h;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, w, h);
            // Os de maior certeza por cima: desenha do baixo para o forte.
            [...own].reverse().forEach(mark => drawMark(ctx, mark, w, h));
            img.dataset.originalSrc = img.src;
            img.dataset.annotatedSrc = canvas.toDataURL('image/jpeg', 0.92);
            img.src = img.dataset.annotatedSrc;
            img.alt = `${img.alt || 'Imagem do exame'} com ${own.length} ${own.length === 1 ? 'achado marcado' : 'achados marcados'}`;
        }));
    }

    function setExamMarksVisible(imgs, visible) {
        Array.from(imgs).forEach(img => {
            const src = visible ? img.dataset.annotatedSrc : img.dataset.originalSrc;
            if (src) img.src = src;
        });
    }

    function renderFindingsLegend(element, marcacoes, numImages) {
        element.textContent = '';
        const marks = Array.isArray(marcacoes) ? marcacoes : [];
        if (!marks.length) {
            const empty = document.createElement('p');
            empty.className = 'findings-empty';
            empty.textContent = 'Nenhum achado pôde ser localizado com segurança nas imagens enviadas.';
            element.appendChild(empty);
            return;
        }
        const list = document.createElement('ol');
        list.className = 'findings-list';
        marks.forEach(mark => {
            const style = CERTEZA[mark.certeza];
            if (!style) return;
            const item = document.createElement('li');
            item.className = `finding finding-${mark.certeza}`;

            const num = document.createElement('span');
            num.className = 'finding-num';
            num.style.background = style.color;
            num.textContent = String(mark.numero);

            const body = document.createElement('div');
            const title = document.createElement('strong');
            title.textContent = mark.estrutura;
            const level = document.createElement('span');
            level.className = 'finding-level';
            level.style.borderColor = style.color;
            level.textContent = style.label;
            const where = document.createElement('p');
            where.className = 'finding-local';
            where.textContent = (numImages > 1 ? `Imagem ${mark.imagem} · ` : '') + mark.local;

            body.append(title, level, where);
            item.append(num, body);
            list.appendChild(item);
        });
        element.appendChild(list);
    }

    /* Página de resultado: marca as imagens, preenche #findings-legend e liga o
       botão #toggle-marks (mostrar/ocultar). Sem imagens (laudo salvo), só a lista. */
    function initExamFindings(imgs, marcacoes, numImages) {
        imgs = Array.from(imgs || []);
        // Pode rodar de novo na mesma página (teste gratuito: "Nova análise"):
        // nada da análise anterior pode sobrar nas imagens nem no botão.
        imgs.forEach(img => { delete img.dataset.originalSrc; delete img.dataset.annotatedSrc; });
        const toggle = document.getElementById('toggle-marks');
        if (toggle) {
            toggle.hidden = true;
            toggle.setAttribute('aria-pressed', 'true');
            toggle.textContent = 'Ocultar marcações';
            toggle.onclick = null;
        }
        const legend = document.getElementById('findings-legend');
        if (legend) renderFindingsLegend(legend, marcacoes, numImages);
        if (!imgs.length || !Array.isArray(marcacoes) || !marcacoes.length) return Promise.resolve();
        return annotateExamImages(imgs, marcacoes).then(() => {
            if (!toggle || !imgs.some(img => img.dataset.annotatedSrc)) return;
            toggle.hidden = false;
            toggle.onclick = () => {
                const visible = toggle.getAttribute('aria-pressed') !== 'true';
                setExamMarksVisible(imgs, visible);
                toggle.setAttribute('aria-pressed', String(visible));
                toggle.textContent = visible ? 'Ocultar marcações' : 'Mostrar marcações';
            };
        });
    }

    window.initExamFindings = initExamFindings;
    window.LAUDO_CERTEZA = CERTEZA;
    window.annotateExamImages = annotateExamImages;
    window.setExamMarksVisible = setExamMarksVisible;
    window.renderFindingsLegend = renderFindingsLegend;
})();
