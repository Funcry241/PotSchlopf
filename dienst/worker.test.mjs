import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import worker from "./worker.mjs";

function runtime() {
    const database = new DatabaseSync(":memory:");
    database.exec(readFileSync(new URL("./schema.sql", import.meta.url), "utf8"));
    const env = {
        DB: {
            prepare(query) {
                return {
                    bind(...args) {
                        const statement = database.prepare(query);
                        return {
                            first: async () => statement.get(...args) ?? null,
                            all: async () => ({ results: statement.all(...args) })
                        };
                    }
                };
            }
        }
    };
    const request = (path, method = "GET", data) => worker.fetch(
        new Request("https://potschlopf.example" + path, {
            method,
            headers: data === undefined ? {} : { "Content-Type": "application/json" },
            body: data === undefined ? undefined : JSON.stringify(data)
        }),
        env,
    );
    return { database, request };
}

test("alle Gäste können ohne Zugangsdaten schreiben und lesen", async () => {
    const { database, request } = runtime();
    const before = await request("/");
    assert.match(await before.text(), /Otter und sein Bruder lesen wohlwollend mit/);
    const result = await request("/notizen", "POST", { inhalt: "Hallo 🦦", absender: "Maus" });
    assert.equal(result.status, 201);
    assert.equal((await result.json()).folge, 1);
    const notes = await (await request("/notizen")).json();
    assert.equal(notes.notizen[0].inhalt, "Hallo 🦦");
    assert.equal(notes.notizen[0].absender, "Maus");
    assert.equal((await request("/notizen", "DELETE")).status, 405);
    assert.equal((await request("/notizen", "PUT", { inhalt: "Bearbeiten" })).status, 405);
    assert.equal(database.prepare("SELECT COUNT(*) AS anzahl FROM notizen").get().anzahl, 1);
    database.close();
});

test("Gästetexte erscheinen als Text, und Größen- sowie Schreibgrenzen greifen", async () => {
    const { database, request } = runtime();
    assert.equal((await request("/notizen", "POST", { inhalt: "<Grüße> & mehr" })).status, 201);
    const html = await (await request("/")).text();
    assert.match(html, /&lt;Grüße&gt; &amp; mehr/);
    assert.ok(!html.includes("<Grüße>"));
    assert.equal((await request("/notizen", "POST", { inhalt: "🦦".repeat(1100) })).status, 400);
    assert.equal((await request("/notizen", "POST", { inhalt: "x".repeat(9000) })).status, 413);
    assert.equal((await request("/notizen", "POST", { inhalt: "x\u0000y" })).status, 400);
    for (let i = 1; i < 24; i++) {
        assert.equal((await request("/notizen", "POST", { inhalt: "Notiz " + i })).status, 201);
    }
    assert.equal((await request("/notizen", "POST", { inhalt: "Über dem Schreiblimit" })).status, 429);
    assert.equal(database.prepare("SELECT COUNT(*) AS anzahl FROM notizen").get().anzahl, 24);
    database.close();
});

test("Startseite und chronologischer Abruf verwenden denselben Bestand", async () => {
    const { database, request } = runtime();
    for (let i = 1; i <= 13; i++) {
        assert.equal((await request("/notizen", "POST", { inhalt: "Notiz " + i })).status, 201);
    }
    const recent = await (await request("/notizen")).json();
    assert.equal(recent.notizen.length, 12);
    assert.equal(recent.notizen[0].folge, 2);
    assert.equal(recent.naechster_stand, 13);
    const page = await (await request("/notizen?nach=0")).json();
    assert.equal(page.notizen[0].folge, 1);
    assert.equal(page.weitere, true);
    const tail = await (await request("/notizen?nach=" + page.naechster_stand)).json();
    assert.deepEqual(tail.notizen.map(note => note.folge), [13]);
    assert.equal(tail.weitere, false);
    database.close();
});
