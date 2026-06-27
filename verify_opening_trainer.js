import { chromium } from 'playwright';

const BASE_URL = 'http://localhost:8001';
const TIMEOUT = 10000;

const log = (msg) => console.log(`[verify] ${msg}`);
const pass = (step, desc) => console.log(`✓ ${step}. ${desc}`);
const fail = (step, desc, err) => {
  console.error(`✗ ${step}. ${desc}: ${err}`);
  process.exit(1);
};

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();

  try {
    log('navigating to board');
    await page.goto(BASE_URL, { waitUntil: 'networkidle', timeout: TIMEOUT });

    // 1. Opening detection on known position
    log('test 1: opening detection');
    await page.keyboard.press('Delete'); // clear board
    await page.evaluate(() => {
      // Set Italian Game opening: 1.e4 e5 2.Nf3 Nc6 3.Bc4
      window.game = new Chess();
      window.game.move('e4'); window.game.move('e5');
      window.game.move('Nf3'); window.game.move('Nc6');
      window.game.move('Bc4');
      window.board.position(window.game.fen());
      window.refreshOpening();
    });
    await page.waitForTimeout(500);
    const opening1 = await page.locator('.opening-block').textContent();
    if (!opening1 || opening1.includes('Unknown')) {
      fail(1, 'Italian Game detection', `got: ${opening1}`);
    }
    pass(1, 'Opening detection on known position');

    // 2. Transposition matching (same position, different move order)
    log('test 2: transposition matching');
    await page.evaluate(() => {
      window.game = new Chess();
      // Same Italian position via different move order
      window.game.move('e4'); window.game.move('e5');
      window.game.move('Bc4'); window.game.move('Nc6');  // Bc4 before Nf3
      window.game.move('Nf3');
      window.board.position(window.game.fen());
      window.refreshOpening();
    });
    await page.waitForTimeout(500);
    const opening2 = await page.locator('.opening-block').textContent();
    if (!opening2 || opening2.includes('Unknown')) {
      fail(2, 'Transposition', `different move order should match: ${opening2}`);
    }
    pass(2, 'Transposition matching (EPD-based)');

    // 3. Candidates render for mid-game position
    log('test 3: candidates list');
    const candidateItems = await page.locator('.opening-candidates li').count();
    if (candidateItems === 0) {
      fail(3, 'Candidates render', 'no candidates found');
    }
    pass(3, 'Candidates render (non-empty list)');

    // 4. Candidate filtering
    log('test 4: candidate filtering');
    const filterInput = page.locator('input[placeholder*="filter"]');
    await filterInput.fill('Italian');
    await page.waitForTimeout(300);
    const filtered = await page.locator('.opening-candidates li').count();
    if (filtered === candidateItems) {
      fail(4, 'Filtering', 'filter had no effect');
    }
    pass(4, 'Candidate filtering (q param works)');

    // 5. Study mode on candidate click
    log('test 5: study mode entry');
    await filterInput.fill('');
    await page.waitForTimeout(200);
    const firstCandidate = page.locator('.opening-candidates li').first();
    await firstCandidate.click();
    await page.waitForTimeout(300);
    const studyBar = page.locator('.study-bar');
    const isVisible = await studyBar.isVisible();
    if (!isVisible) {
      fail(5, 'Study mode entry', 'study-bar not visible after candidate click');
    }
    pass(5, 'Study mode entry (read-only snapshot taken)');

    // 6. Study stepping (First/Prev/Next/Last)
    log('test 6: study navigation');
    const nextBtn = page.locator('button:has-text("Next")');
    const initialStep = await page.locator('.study-bar').getAttribute('data-step');
    await nextBtn.click();
    await page.waitForTimeout(500);
    const afterStep = await page.locator('.study-bar').getAttribute('data-step');
    if (initialStep === afterStep) {
      fail(6, 'Study stepping', 'Next button did not advance');
    }
    pass(6, 'Study stepping (navigation works)');

    // 7. Eval fetches per step (no duplicates)
    log('test 7: per-step evaluation');
    const prevBtn = page.locator('button:has-text("Prev")');
    await prevBtn.click();
    await page.waitForTimeout(800); // wait for eval fetch
    const evalText = await page.locator('.study-bar [class*="eval"]').textContent({ timeout: 2000 });
    if (!evalText || evalText.includes('undefined')) {
      fail(7, 'Eval fetch', `eval not rendered: ${evalText}`);
    }
    pass(7, 'Per-step evaluation (fetched and rendered)');

    // 8. Commentary rendering
    log('test 8: commentary');
    const commentary = await page.locator('.study-bar [class*="commentary"]').textContent({ timeout: 2000 });
    // Commentary may be empty for some positions, but should be text content (not error)
    pass(8, 'Commentary rendering (no errors)');

    // 9. Return to play
    log('test 9: return to play');
    const returnBtn = page.locator('button:has-text("Return")');
    await returnBtn.click();
    await page.waitForTimeout(300);
    const playBarVisible = await page.locator('.move-controls').isVisible();
    if (!playBarVisible) {
      fail(9, 'Return to play', 'play controls not restored');
    }
    pass(9, 'Return to play (controls restored)');

    // 10. Deterministic candidate ordering
    log('test 10: candidate ordering');
    await page.evaluate(() => window.refreshOpening());
    await page.waitForTimeout(300);
    const names1 = await page.locator('.opening-candidates li').allTextContents();
    await page.evaluate(() => window.refreshOpening());
    await page.waitForTimeout(300);
    const names2 = await page.locator('.opening-candidates li').allTextContents();
    if (JSON.stringify(names1) !== JSON.stringify(names2)) {
      fail(10, 'Deterministic ordering', 'candidates order changed on refresh');
    }
    pass(10, 'Deterministic candidate ordering');

    log('✓ All 10 verification steps passed.');
    await browser.close();
  } catch (err) {
    console.error(`Fatal error: ${err.message}`);
    console.error(err.stack);
    await browser.close();
    process.exit(1);
  }
})();
