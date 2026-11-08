import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { Link } from 'react-router-dom';
import { Card, Button, Row, Col, Skeleton, message } from 'antd';
import { LoadingOutlined, ExclamationCircleOutlined } from '@ant-design/icons';

const { Meta } = Card;

const HomePage: React.FC = () => {
  const [mangos, setMangos] = useState<{ [key: string]: any }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const fetchMangos = async () => {
      try {
        const response = await axios.get('https://api.mangodelivery.com/mangos');
        setMangos(response.data);
      } catch (err) {
        setError('Failed to fetch mangos');
        message.error('Failed to fetch mangos');
      } finally {
        setLoading(false);
      }
    };

    fetchMangos();
  }, []);

  if (loading) return <Skeleton active />;
  if (error) return <ExclamationCircleOutlined style={{ fontSize: 24 }} />;

  return (
    <div style={{ padding: '24px' }}>
      <h1>Mango Delivery</h1>
      <Row gutter={16}>
        {mangos.map((mango) => (
          <Col key={mango.id} xs={24} sm={12} md={8} lg={6}>
            <Card
              title={mango.name}
              cover={<img alt={mango.name} src={mango.image} />}
              actions={[
                <Link key="buy" to={`/mangos/${mango.id}`}>
                  Buy
                </Link>,
              ]}
            >
              <Meta description={mango.description} />
              <p>Price: ${mango.price.toFixed(2)}</p>
            </Card>
          </Col>
        ))}
      </Row>
    </div>
  );
};

export default HomePage;
