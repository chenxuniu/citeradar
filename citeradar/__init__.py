"""
CiteRadar — Automated Citation Intelligence for Google Scholar Profiles.

Pipeline:
  1. scraper  — fetch author's own papers from a Scholar profile
  2. tracker  — find all papers that cite those papers
  3. profiler — resolve full author metadata (institution / city / country)
  4. ranker   — rank citing researchers by citation count and h-index
  5. reporter — generate a text summary and an interactive HTML world map
"""

__version__ = "1.0.0"
__author__ = "CiteRadar"
