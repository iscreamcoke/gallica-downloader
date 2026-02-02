# Gallica → PDF Downloader

Télécharge automatiquement **n’importe quel livre Gallica (BnF)** en **PDF haute qualité**, à partir d’une URL ou d’un ARK.

---

## Installation

### 1. Python requis
Python ≥ 3.9

```bash
python3 --version
```

### 2. (Recommandé) meilleure génération PDF

```bash
python3 -m pip install img2pdf
```

Sinon fallback automatique sur Pillow :

```bash
python3 -m pip install pillow
```

---

## Utilisation

### Depuis une URL Gallica

```bash
python3 gallica.py --url "https://gallica.bnf.fr/ark:/12148/bd6t542069393/f1.item"
```

### Depuis un ARK

```bash
python3 gallica.py --ark bd6t542069393
```

Résultat :

```
bd6t542069393.pdf
```

---

## Options

| Option | Description | Défaut |
|-------|-------------|----------|
| `--url` | URL Gallica complète | — |
| `--ark` | ARK seul | — |
| `--workers` | Téléchargements parallèles | 4 |
| `--max-width` | Largeur IIIF (qualité) | 2000 |
| `--sleep` | Pause entre requêtes | 0 |
| `--out` | Nom du PDF final | `<ark>.pdf` |
| `--dir` | Dossier temporaire | `gallica_<ark>` |
| `--keep` | Conserver les images | supprimé par défaut |

---

## Exemples

### Plus rapide (8 threads)

```bash
python3 gallica.py --ark bd6t542069393 --workers 8
```

### Qualité maximale

```bash
python3 gallica.py --ark bd6t542069393 --max-width 3000
```

### Conserver les images (debug)

```bash
python3 gallica.py --ark bd6t542069393 --keep
```

---

## Performance conseillée

Valeurs stables :

```
--workers 4 à 6
--sleep 0
```

Réseau très rapide :

```
--workers 8
```

Éviter >10 (Gallica peut bloquer).

---

## Problèmes possibles

### 403 Forbidden
Gallica bloque parfois les requêtes automatisées.

Solution :

1. ouvrir la page dans le navigateur
2. relancer le script

ou changer de réseau (4G/VPN).

---

### Téléchargement lent
Augmenter :

```bash
--workers 6
```

---

## Licence

MIT License

Respecter les conditions d’utilisation Gallica :  
https://gallica.bnf.fr
