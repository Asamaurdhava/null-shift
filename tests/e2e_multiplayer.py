import os
import re

from playwright.sync_api import expect, sync_playwright

URL=os.environ.get('NULLSHIFT_TEST_URL','http://127.0.0.1:8765').rstrip('/')+'/'

def main():
    with sync_playwright() as p:
        launch_options={'headless':True}
        if executable_path:=os.environ.get('CHROMIUM_EXECUTABLE'):
            launch_options['executable_path']=executable_path
        browser=p.chromium.launch(**launch_options)
        c1=browser.new_context(viewport={"width":1280,"height":900})
        c2=browser.new_context(viewport={"width":390,"height":844})
        host=c1.new_page(); guest=c2.new_page()
        host.goto(URL); guest.goto(URL)
        expect(host.locator('#connection')).to_contain_text('LIVE', timeout=5000)
        expect(guest.locator('#connection')).to_contain_text('LIVE', timeout=5000)

        host.locator('#nameInput').fill('Alpha')
        host.locator('#primaryBtn').click()
        active_class=re.compile(r'(?:^|\s)active(?:\s|$)')
        hidden_class=re.compile(r'(?:^|\s)hidden(?:\s|$)')
        shielded_class=re.compile(r'(?:^|\s)shielded(?:\s|$)')
        expect(host.locator('#lobby')).to_have_class(active_class, timeout=5000)
        code=host.locator('#copyCode').inner_text().strip()
        assert len(code)==4, code

        guest.locator('[data-tab="join"]').click()
        guest.locator('#nameInput').fill('Bravo')
        guest.locator('#codeInput').fill(code)
        guest.locator('#primaryBtn').click()
        expect(host.locator('#lobbyPlayers')).to_contain_text('Bravo', timeout=5000)
        expect(guest.locator('#lobbyPlayers')).to_contain_text('Alpha', timeout=5000)

        host.locator('#startBtn').click()
        expect(host.locator('#game')).to_have_class(active_class, timeout=5000)
        expect(guest.locator('#game')).to_have_class(active_class, timeout=5000)
        expect(host.locator('#countdown')).not_to_have_class(hidden_class, timeout=3000)
        expect(host.locator('#countdown')).to_have_class(hidden_class, timeout=7000)

        host.locator('.node').nth(0).click()
        expect(guest.locator('#scoreboard')).to_contain_text('1 NODES', timeout=4000)
        # Confirm owner color arrives on both clients.
        host_style=host.locator('.node').nth(0).get_attribute('style')
        guest_style=guest.locator('.node').nth(0).get_attribute('style')
        assert host_style==guest_style and '#50efff' in host_style.lower(), (host_style, guest_style)

        guest.locator('.node').nth(0).click()
        bravo_row=host.locator('.score-row').filter(has_text='Bravo')
        expect(bravo_row).to_contain_text('1 NODES', timeout=4000)

        # Host captures another node and shields it.
        host.locator('.node').nth(1).click()
        expect(guest.locator('#scoreboard')).to_contain_text('Alpha', timeout=3000)
        host.locator('.node').nth(1).click()  # select owned node; capture won't resend for self
        host.locator('[data-power="shield"]').click()
        expect(guest.locator('.node').nth(1)).to_have_class(shielded_class, timeout=4000)

        # Refresh guest and verify reconnect to same active match.
        guest.reload()
        expect(guest.locator('#game')).to_have_class(active_class, timeout=6000)
        expect(guest.locator('#gameRoom')).to_have_text(code)
        expect(guest.locator('#secretText')).not_to_have_text('Mission encrypted')

        print('E2E PASS', code)
        browser.close()

if __name__=='__main__': main()
