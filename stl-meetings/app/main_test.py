#!/usr/bin/env python3
import os, re, sqlite3, hashlib, smtplib, logging, threading, time
os.environ["DYLD_LIBRARY_PATH"] = "/opt/homebrew/lib"
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote
import requests
from icalendar import Calendar
from bs4 import BeautifulSoup
from weasyprint import HTML
from flask import Flask, render_template, request, jsonify
import schedule

