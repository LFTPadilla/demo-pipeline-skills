# The comment layer

The layer is two files: `assets/layer/layer.css` and `assets/layer/layer.js`. Both skills use them.
`mockup-review` builds them into the page (`scripts/inject.py`, at build or deploy time). `live-app-review` serves them at `/__review/layer.css` and `/__review/layer.js`.

## Keys and buttons

| Action | How |
|---|---|
| Toggle Browse and Comment mode | `C` |
| Close popover, then panel | `Esc`. Text that you did not save stays for the next composer |
| Previous or next comment | `[` and `]` |
| Open a given screen | Add `#screen=<id>` to the link |
| Open a given comment | Add `#comment=<id>` to the link. The Link button in a thread copies it |
| Filter, version, export, import | Panel button "Comments (n)" |
| Save a comment or a reply | `Enter`, or `Ctrl+Enter`. `Shift+Enter` adds a line |

## What a click can comment on

1. A click on an element anchors the comment to that element.
2. `html`, `body`, and a container that fills half of the viewport count as empty space. A click there anchors to the nearest element in that area. The pin sits on the edge of that element.
3. The outline always shows the element that a click would use. It follows the pointer, and it hides when the pointer leaves the window or the iframe, and when the iframe scrolls.
4. A pin on an element follows that element on scroll and resize. A pin never anchors to `body`.
5. In frame mode, the wrapper area around a small viewport (Tablet, Mobile) is not the app. It takes no comment.
6. In frame mode, the button "Page comment" adds a comment on the whole page. It has no element.

## Features

1. The reviewer name is asked once, in the first composer. The toolbar then shows "Commenting as <name>". The pencil edits it. Old comments keep the name they were written with.
2. Times show as relative time. The title of the time shows the full date.
3. The toolbar shows "N new" for comments and replies of other people since the last time the panel was open.
4. The panel lists comments under one sticky header per screen. Numbers count inside a screen. Long text is cut to 3 lines with a Show more button. The thread view shows all of it.
5. Every comment and reply takes the reactions 👍 ✅ 👀 ❤️ 🎉 ❓. A click toggles your reaction. The title of a chip lists the names. The 😀 button in a text box inserts an emoji at the cursor.
6. The export shows reactions after each item, for example `[👍 2 · ✅ 1]`.

## Config keys that the layer reads

The layer reads `cmt-config`. `live-app-review` builds it from `review.json`. A static mockup holds it in the page.

| Key | Meaning |
|---|---|
| `startMode` | `"comment"` (default) or `"browse"`. The mode of a reviewer on the first visit. A saved choice wins. With `"browse"`, the one-time hint says `Press C or click Comment to leave feedback.` |
| `frame.screens[].prefill` | Frame mode only. Steps that fill the app, such as a search box. See `live-app-review/references/config.md` |

## window.cmt

The layer sets `window.cmt` with `exportMarkdown()`, `exportJSON()`, `importJSON(obj)`, `mode()`, `screen()`, `meta()`, and `viewport()`.
`mode()` returns `shared` when the comments API answers, and `local` otherwise.
In a static mockup, `meta()` returns `null` and `viewport()` returns `desktop`.

## Pin numbers

1. The comment number `n` is set when the comment is created and never changes.
2. Numbers count per screen and per version. A number is never used twice.
3. The layer and the server each remember the highest number used, so a delete leaves a gap, also when it removes the newest comment.
4. In shared mode the server sets `n`. In local mode the browser sets it.
5. A comment without `n` (from an older layer) gets its number once, in creation order.

## Change the layer

1. Edit `assets/layer/layer.css` and `assets/layer/layer.js` only. No template holds a copy.
2. Run `uv run --with playwright python SKILL/scripts/test_layer.py`.
3. Run `uv run --with playwright python ../live-app-review/scripts/test_browser.py`.
4. Both tests load the canonical files. A change shows in both skills at once.
