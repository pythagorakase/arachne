# Decision templates

These files are trusted producer-side templates, not application chrome and not
server routes. A producer renders a self-contained HTML brief, then publishes
that output through `bin/publish-page.py` like any other Arachne decision.

## Blind image comparison

`bin/build-image-review.py` consumes one JSON manifest per subject. Each pose
contains exactly two or three candidate images. Two candidates yield one A/B
or tie decision. Three candidates appear once in a best-to-worst ranking board,
with drag ordering and mobile-safe move buttons. Only one pose is visible at a
time; the reviewer answers, may leave an optional comment, and advances through
the brief. Earlier winners accumulate in a responsive consistency rail: vertical
on wider screens and horizontally scrollable on mobile. Candidate order and A/B
assignment are randomized once at build time and preserved in a private
provenance sidecar. A direct three-item ranking is complete in one pass and does
not require repeated pairwise or Elo matchups.

```json
{
  "schema_version": 1,
  "issue": "project-character-reference-review-v1",
  "title": "Character reference review",
  "subject": {"id": "character", "label": "Character"},
  "instructions": "Choose the stronger identity reference.",
  "poses": [
    {
      "id": "s01-frontal",
      "label": "Neutral frontal close-up",
      "candidates": [
        {"id": "iteration-1", "path": "./candidate-1.png"},
        {"id": "iteration-2", "path": "./candidate-2.png"}
      ]
    }
  ]
}
```

Build and publish:

```bash
bin/build-image-review.py manifest.json decision_character_review.html
bin/publish-page.py decision_character_review.html --pages-dir pages
```

The HTML contains compact metadata-free JPEG previews and opaque candidate IDs, but
neither source filenames nor filesystem paths. Keep the generated
`*.provenance.json` private: it is the mapping needed to interpret the ruling.
The Arachne server and ruling protocol require no image-specific changes.

## Blind image rating

`bin/build-image-rating.py` consumes one JSON manifest containing two or more
images to rate independently. The builder shuffles the set once, assigns only
positional labels after shuffling, and shows one image at a time with a fixed
thumbnail rail. Source IDs and paths appear only in the private provenance
sidecar. Byte-identical images are allowed so a set can include deliberate
positive and negative controls.

```json
{
  "schema_version": 1,
  "issue": "project-character-identity-rating-v1",
  "title": "Character identity rating",
  "subject": "Character",
  "question": "Does this face look like Character?",
  "instructions": "Judge facial geometry and recognition, not polish.",
  "item_noun": "Face",
  "items": [
    {"id": "positive-control", "path": "./control.png"},
    {"id": "candidate-iteration", "path": "./candidate.png"}
  ],
  "options": [
    {"value": "yes", "label": "Yes", "hint": "Clearly recognizable."},
    {"value": "kind_of", "label": "Kind of", "hint": "Recognizable but drifted."},
    {"value": "no", "label": "No", "hint": "Not recognizable."}
  ]
}
```

`instructions`, `item_noun`, and `options` are optional. The defaults are a
short rating instruction, `Item`, and Yes / Kind of / No. Item and option IDs
must be safe identifiers; all object schemas reject unknown keys.

Build and publish:

```bash
bin/build-image-rating.py manifest.json decision_character_rating.html
bin/publish-page.py decision_character_rating.html --pages-dir pages
```

The generated page embeds compact metadata-free previews and exposes only
opaque item IDs. Keep its `*.provenance.json` private; it records the shuffled
position-to-source mapping needed to interpret the rating.
