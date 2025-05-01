
from flask import Flask, request, jsonify
import psycopg2

app = Flask(__name__)

def get_distance_km(lon1, lat1, lon2, lat2):
    # fake method to calc distance in km from point 1 to point 2 (using google maps api for example)
    return 1.0

def notify_driver(driver_id, request_id):
    # fake method to send the offer (ride request) to the driver
    return None

def calculate_fare(distance_km, duration_min, vehicle_type, surge_mul):
    type_surcharge = {'economy': 1.00, 'premium': 1.10, 'family': 1.25}[vehicle_type]
    subtotal = (distance_km * 1.5) + (duration_min * 0.5)
    fare = subtotal * type_surcharge * surge_mul
    return round(fare, 2)

def get_db_connection():
    return psycopg2.connect(
        dbname='rideconnect',
        user='postgres',
        password='123123',
        host='localhost'
    )

@app.route('/ride_requests', methods=['POST'])
def create_ride_request():
    data = request.get_json()
    rider_id = data['rider_id']
    plon, plat = data['pickup_lon'], data['pickup_lat']
    dlon, dlat = data['dropoff_lon'], data['dropoff_lat']
    vtype = data['preferred_type']

    dist_km = get_distance_km(plon, plat, dlon, dlat)
    dur_min = (dist_km / 40.0) * 60.0 # vehicle  speed is 40 km/h

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        conn.autocommit = False

        cur.execute(
            """
            SELECT multiplier
            FROM surge_areas
            WHERE ST_Contains(
                region,
                ST_SetSRID(ST_MakePoint(%s, %s),4326)
            )
            LIMIT 1
            """, (plon, plat)
        )
        row = cur.fetchone()
        surge_mul = row['multiplier'] if row else 1.0

        est_price = calculate_fare(dist_km, dur_min, vtype, surge_mul)

        cur.execute(
            """
            INSERT INTO ride_requests(
                request_id, rider_id,
                pickup_geom, dropoff_geom,
                est_price, est_duration_min,
                surge_area_id, preferred_vehicle_type,
                request_status
            ) VALUES (
                gen_random_uuid(), %s,
                ST_SetSRID(ST_MakePoint(%s, %s),4326),
                ST_SetSRID(ST_MakePoint(%s, %s),4326),
                %s, %s,
                (SELECT area_id
                FROM surge_areas
                WHERE ST_Contains(
                    region,
                    ST_SetSRID(ST_MakePoint(%s, %s),4326)
                )
                LIMIT 1
            ),
            %s, 'requested'
            )
            RETURNING request_id
        """, (
            rider_id, plon, plat, dlon, dlat,
            est_price, dur_min, plon, plat, vtype
        ))
        req_id = cur.fetchone()['request_id']

        cur.execute(
            """
            SELECT d.user_id AS driver_id
                FROM drivers d
                JOIN vehicles v ON v.driver_id = d.user_id
            WHERE d.status = 'online'
                AND v.type   = %s
                AND ST_DWithin(
                    d.current_location::geography,
                    ST_SetSRID(ST_MakePoint(%s, %s),4326)::geography,
                    5000
                )
            ORDER BY d.current_location <-> ST_SetSRID(ST_MakePoint(%s, %s),4326)
            LIMIT 5
            """, (vtype, plon, plat, plon, plat)
        )
        candidates = [r['driver_id'] for r in cur.fetchall()]

        if not candidates:
            conn.rollback()
            return jsonify({
                "error": "No drivers available within 5 km"
            }), 404
        
        for did in candidates:
            notify_driver(did, req_id)

        conn.commit()
        return jsonify({
            "request_id": req_id,
            "est_price": est_price,
            "est_duration_min": dur_min
        }), 201

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cur.close()
        conn.close()



@app.route('/ride_requests/<request_id>/accept', methods=['POST'])
def accept_ride(request_id):
    data = request.get_json()
    driver_id = data['driver_id']

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        conn.autocommit = False

        cur.execute(
            """
            UPDATE ride_requests
                SET request_status = 'accepted'
            WHERE request_id = %s
                AND request_status = 'requested'
            RETURNING preferred_vehicle_type, pickup_geom
            """, request_id
        )
        req = cur.fetchone()
        if not req:
            conn.rollback()
            return jsonify({"error": "Ride already accepted or invalid"}), 409

        cur.execute(
            """
            SELECT d.user_id
                FROM drivers d
                JOIN vehicles v ON v.driver_id = d.user_id
            WHERE d.user_id = %s
                AND d.status  = 'online'
                AND v.type    = %s
                AND ST_DWithin(
                    d.current_location::geography,
                    %s::geography,
                    5000
                )
            FOR UPDATE SKIP LOCKED
            """, (
                driver_id,
                req['preferred_vehicle_type'],
                req['pickup_geom']
            )
        )
        drv = cur.fetchone()
        if not drv:
            conn.rollback()
            return jsonify({"error": "Driver unavailable"}), 404

        cur.execute(
            """
            INSERT INTO rides(
                ride_id, request_id, driver_id,
                status, accepted_at
            ) VALUES (
                gen_random_uuid(), %s, %s, 'accepted', now()
            )
            RETURNING ride_id
            """, (request_id, driver_id)
        )
        ride_id = cur.fetchone()['ride_id']

        cur.execute("UPDATE drivers SET status = 'busy' WHERE user_id = %s", driver_id)

        conn.commit()
        return jsonify({
            "ride_id": ride_id,
            "driver_id": driver_id,
            "message": "Ride accepted successfully"
        }), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cur.close()
        conn.close()

if __name__ == '__main__':
    app.run(debug=True, port=6666)

