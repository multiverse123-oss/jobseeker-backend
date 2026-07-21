const puppeteer = require('puppeteer');
(async () => {
  const query = process.argv[2];
  const location = process.argv[3] || 'United States';
  const browser = await puppeteer.launch({ headless: "new", args: ['--no-sandbox', '--disable-setuid-sandbox'] });
  const page = await browser.newPage();
  await page.goto(`https://www.google.com/search?q=jobs+${encodeURIComponent(query)}&location=${encodeURIComponent(location)}&hl=en`, { waitUntil: 'networkidle2' });
  const jobs = await page.evaluate(() => {
    const cards = document.querySelectorAll('div[data-hveid]');
    return Array.from(cards).map(card => ({
      title: card.querySelector('h2')?.innerText || '',
      company: card.querySelector('div > div > div > div > span')?.innerText || '',
      link: card.querySelector('a')?.href || ''
    }));
  });
  console.log(JSON.stringify(jobs));
  await browser.close();
})();
