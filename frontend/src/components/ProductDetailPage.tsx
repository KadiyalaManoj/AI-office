import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { useParams } from 'react-router-dom';
import { Card, Button, Row, Col, Skeleton, message } from 'antd';
import { LoadingOutlined, ExclamationCircleOutlined } from '@ant-design/icons';

const { Meta } = Card;

const ProductDetailPage: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const [mango, setMango] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const fetchMango = async () => {
      try {
        const response = await axios.get(`https://api.mangodelivery.com/mangos/${id}`);
        setMango(response.data);
      } catch (err) {
        setError('Failed to fetch mango');
        message.error('Failed to fetch mango');
      } finally {
        setLoading(false);
      }
    };

    fetchMango();
  }, [id]);

  if (loading) return <Skeleton active />;
  if (error) return <ExclamationCircleOutlined style={{ fontSize: 24 }} />;

  return (
    <div style={{ padding: '24px' }}>
      <h1>{mango.name}</h1>
      <Row gutter={16}>
        <Col xs={24} sm={12} md={8} lg={6}>
          <Card
            cover={<img alt={mango.name} src={mango.image} />}
          >
            <Meta description={mango.description} />
            <p>Price: ${mango.price.toFixed(2)}</p>
          </Card>
        </Col>
        <Col xs={24} sm={12} md={8} lg={6}>
          <Button type="primary" block href={`/cart/add/${id}`}>
            Add to Cart
          </Button>
        </Col>
      </Row>
    </div>
  );
};

export default ProductDetailPage;
