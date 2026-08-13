import os
import time
from datetime import datetime
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

AQI_LABELS = {1: 'Good', 2: 'Fair', 3: 'Moderate', 4: 'Poor', 5: 'Very Poor'}

AQI_DESCRIPTIONS = {
    1: "Air quality is satisfactory, and air pollution poses little or no risk. Enjoy your outdoor activities!",
    2: "Air quality is acceptable. However, there may be a risk for some people who are unusually sensitive to air pollution.",
    3: "Members of sensitive groups may experience health effects. The general public is less likely to be affected.",
    4: "The air has reached a high level of pollution and is unhealthy for sensitive groups. Reduce prolonged outdoor exertion.",
    5: "Health alert: The risk of health effects is increased for everyone. Avoid all outdoor physical activities."
}

CACHE_TTL = 600  # 10 minutes TTL in seconds
WEATHER_CACHE = {}  # In-memory dictionary cache: key -> (timestamp, data_dict)


def pm25_to_aqi(pm25):
    """Convert PM2.5 concentration (µg/m³) to US EPA AQI numeric value (0–500)."""
    if pm25 is None:
        return None
    pm25 = float(pm25)
    breakpoints = [
        (0.0, 12.0, 0, 50),
        (12.1, 35.4, 51, 100),
        (35.5, 55.4, 101, 150),
        (55.5, 150.4, 151, 200),
        (150.5, 250.4, 201, 300),
        (250.5, 350.4, 301, 400),
        (350.5, 500.4, 401, 500),
    ]
    for bp_lo, bp_hi, aqi_lo, aqi_hi in breakpoints:
        if pm25 <= bp_hi:
            aqi = ((aqi_hi - aqi_lo) / (bp_hi - bp_lo)) * (pm25 - bp_lo) + aqi_lo
            return round(aqi)
    return 500  # Above highest breakpoint


def parse_forecast(forecast_data):
    """Parse 5-day/3-hour forecast into one entry per day (next 5 days)."""
    if not forecast_data or 'list' not in forecast_data:
        return []

    daily = {}
    for entry in forecast_data['list']:
        dt_txt = entry.get('dt_txt', '')
        date_str = dt_txt.split(' ')[0]  # "YYYY-MM-DD"
        if date_str not in daily:
            daily[date_str] = {
                'temps': [],
                'entry': None
            }
        temp = entry.get('main', {}).get('temp')
        if temp is not None:
            daily[date_str]['temps'].append(temp)
        # Prefer noon (12:00) slot for representative icon/description
        hour = dt_txt.split(' ')[1] if ' ' in dt_txt else ''
        if hour == '12:00:00' or daily[date_str]['entry'] is None:
            if hour == '12:00:00':
                daily[date_str]['entry'] = entry

    # If no noon entry found, use the first entry for that day
    for date_str in daily:
        if daily[date_str]['entry'] is None:
            for entry in forecast_data['list']:
                if entry.get('dt_txt', '').startswith(date_str):
                    daily[date_str]['entry'] = entry
                    break

    # Build sorted list, skip today, take next 5
    today_str = datetime.now().strftime('%Y-%m-%d')
    sorted_dates = sorted(d for d in daily.keys() if d != today_str)[:5]

    forecast_list = []
    for date_str in sorted_dates:
        info = daily[date_str]
        entry = info['entry']
        if not entry:
            continue
        dt_obj = datetime.strptime(date_str, '%Y-%m-%d')
        weather_item = entry.get('weather', [{}])[0]
        forecast_list.append({
            'day': dt_obj.strftime('%a'),           # "Mon", "Tue"
            'date_short': dt_obj.strftime('%b %d'),  # "Jan 15"
            'icon': weather_item.get('icon', '01d'),
            'description': weather_item.get('description', ''),
            'temp_max': round(max(info['temps'])) if info['temps'] else '--',
            'temp_min': round(min(info['temps'])) if info['temps'] else '--',
        })

    return forecast_list


@app.route('/', methods=['GET', 'POST'])
def index():
    weather = None
    error = None
    wind_speed = None
    humidity = None
    temp_max = None
    temp_min = None
    clouds = None
    aqi = None
    aqi_label = None
    aqi_description = None
    aqi_numeric = None
    pm2_5 = None
    condition = None
    country = None
    forecast = []

    if request.method == 'POST':
        city = (request.form.get('city') or '').strip()
        if not city:
            error = "Please enter a valid city name."
            return render_template('index.html', error=error)

        api_key = os.getenv('OPENWEATHER_API_KEY')
        if not api_key:
            error = "Server configuration error: OpenWeather API key missing."
            return render_template('index.html', error=error)

        cache_key = city.lower()
        now = time.time()

        # Check in-memory cache
        if cache_key in WEATHER_CACHE:
            cached_time, cached_data = WEATHER_CACHE[cache_key]
            if now - cached_time < CACHE_TTL:
                print(f"[CACHE HIT] Serving '{city}' from in-memory cache.")
                return render_template('index.html', **cached_data)

        url = f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric"

        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                weather = response.json()

                # Extract condition & country
                if weather.get('weather'):
                    condition = weather['weather'][0].get('main', '').lower()
                country = weather.get('sys', {}).get('country', '')

                # Extract additional weather details
                wind_speed = weather.get('wind', {}).get('speed')
                humidity = weather.get('main', {}).get('humidity')
                temp_max = weather.get('main', {}).get('temp_max')
                temp_min = weather.get('main', {}).get('temp_min')
                clouds = weather.get('clouds', {}).get('all')

                # Fetch Air Quality Index and PM2.5 using coordinates
                lat = weather.get('coord', {}).get('lat')
                lon = weather.get('coord', {}).get('lon')
                if lat is not None and lon is not None:
                    # Air Pollution API
                    aqi_url = f"http://api.openweathermap.org/data/2.5/air_pollution?lat={lat}&lon={lon}&appid={api_key}"
                    try:
                        aqi_response = requests.get(aqi_url, timeout=5)
                        if aqi_response.status_code == 200:
                            aqi_data = aqi_response.json()
                            item = aqi_data.get('list', [{}])[0]
                            aqi = item.get('main', {}).get('aqi')
                            aqi_label = AQI_LABELS.get(aqi, 'N/A')
                            aqi_description = AQI_DESCRIPTIONS.get(aqi, '')
                            pm2_5 = item.get('components', {}).get('pm2_5')
                            aqi_numeric = pm25_to_aqi(pm2_5)
                    except Exception as e:
                        print(f"Air Pollution API error: {e}")

                    # 5-Day Forecast API
                    forecast_url = f"http://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={api_key}&units=metric"
                    try:
                        forecast_response = requests.get(forecast_url, timeout=5)
                        if forecast_response.status_code == 200:
                            forecast_data = forecast_response.json()
                            forecast = parse_forecast(forecast_data)
                    except Exception as e:
                        print(f"Forecast API error: {e}")

                render_data = {
                    'weather': weather,
                    'error': None,
                    'wind_speed': wind_speed,
                    'humidity': humidity,
                    'temp_max': temp_max,
                    'temp_min': temp_min,
                    'clouds': clouds,
                    'aqi': aqi,
                    'aqi_label': aqi_label,
                    'aqi_description': aqi_description,
                    'aqi_numeric': aqi_numeric,
                    'pm2_5': pm2_5,
                    'condition': condition,
                    'country': country,
                    'forecast': forecast
                }

                # Store in cache
                WEATHER_CACHE[cache_key] = (now, render_data)
                return render_template('index.html', **render_data)

            elif response.status_code == 404:
                error = f"City '{city}' not found. Please check spelling."
            else:
                error = "Unable to fetch weather data. Please try again later."
        except requests.exceptions.Timeout:
            error = "Request timed out while connecting to weather service."
        except requests.exceptions.RequestException:
            error = "Network error: Unable to reach weather service."

    return render_template('index.html', weather=weather, error=error,
                           wind_speed=wind_speed, humidity=humidity,
                           temp_max=temp_max, temp_min=temp_min,
                           clouds=clouds, aqi=aqi, aqi_label=aqi_label,
                           aqi_description=aqi_description, aqi_numeric=aqi_numeric,
                           pm2_5=pm2_5, condition=condition, country=country,
                           forecast=forecast)


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


if __name__ == '__main__':
    app.run(debug=False, port=5000)